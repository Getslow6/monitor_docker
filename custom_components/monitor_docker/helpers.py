"""Monitor Docker API helper."""

import asyncio
import concurrent
import logging
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import aiodocker
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
import homeassistant.util.dt as dt_util
from dateutil import parser, relativedelta
from homeassistant.const import (
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.discovery import load_platform
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_MEMORY_LIMIT,
    ATTR_ONLINE_CPUS,
    ATTR_VERSION_ARCH,
    ATTR_VERSION_KERNEL,
    ATTR_VERSION_OS,
    ATTR_VERSION_OS_TYPE,
    COMPONENTS,
    CONF_CERTPATH,
    CONF_MEMORYCHANGE,
    CONF_PRECISION_CPU,
    CONF_PRECISION_MEMORY_MB,
    CONF_PRECISION_MEMORY_PERCENTAGE,
    CONF_PRECISION_NETWORK_KB,
    CONF_PRECISION_NETWORK_MB,
    CONF_RETRY,
    CONTAINER,
    CONTAINER_INFO_HEALTH,
    CONTAINER_INFO_IMAGE,
    CONTAINER_INFO_IMAGE_HASH,
    CONTAINER_INFO_NETWORK_AVAILABLE,
    CONTAINER_INFO_STATE,
    CONTAINER_INFO_STATUS,
    CONTAINER_INFO_UPTIME,
    CONTAINER_STATS_1CPU_PERCENTAGE,
    CONTAINER_STATS_CPU_PERCENTAGE,
    CONTAINER_STATS_MEMORY,
    CONTAINER_STATS_MEMORY_PERCENTAGE,
    CONTAINER_STATS_NETWORK_SPEED_DOWN,
    CONTAINER_STATS_NETWORK_SPEED_UP,
    CONTAINER_STATS_NETWORK_TOTAL_DOWN,
    CONTAINER_STATS_NETWORK_TOTAL_UP,
    DOCKER_INFO_CONTAINER_PAUSED,
    DOCKER_INFO_CONTAINER_RUNNING,
    DOCKER_INFO_CONTAINER_STOPPED,
    DOCKER_INFO_CONTAINER_TOTAL,
    DOCKER_INFO_IMAGES,
    DOCKER_INFO_VERSION,
    DOCKER_STATS_1CPU_PERCENTAGE,
    DOCKER_STATS_CPU_PERCENTAGE,
    DOCKER_STATS_MEMORY,
    DOCKER_STATS_MEMORY_PERCENTAGE,
    DOMAIN,
    PRECISION,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


def toKB(value: float, precision: int = PRECISION) -> float:
    """Converts bytes to kBytes."""
    precision = None if precision == 0 else precision
    return round(value / (1024 ** 1), precision)


def toMB(value: float, precision: int = PRECISION) -> float:
    """Converts bytes to MBytes."""
    precision = None if precision == 0 else precision
    return round(value / (1024 ** 2), precision)


#################################################################
class DockerAPI:
    """Docker API abstraction allowing multiple Docker instances beeing monitored."""

    def __init__(self, hass: HomeAssistant, config: ConfigType):
        """Initialize the Docker API."""

        self._hass = hass
        self._config = config
        self._instance: str = config[CONF_NAME]
        self._containers: dict[str, DockerContainerAPI] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._info: dict[str, Any] = {}
        self._event_create: dict[str, int] = {}
        self._event_destroy: dict[str, int] = {}
        self._dockerStopped = False
        self._subscribers: list[Callable] = []
        self._api: aiodocker.Docker = None

        self._tcp_connector = None
        self._tcp_session = None
        self._tcp_ssl_context = None

        _LOGGER.debug("[%s]: Helper version: %s", self._instance, VERSION)

        self._interval: int = config[CONF_SCAN_INTERVAL]
        self._retry_interval: int = config[CONF_RETRY]
        _LOGGER.debug(
            "[%s]: CONF_SCAN_INTERVAL=%d, RETRY=%d",
            self._instance,
            self._interval,
            self._retry_interval,
        )

    #############################################################
    async def init(self, startCount: int = 0):
        # Set to None when called twice, etc
        self._api = None

        _LOGGER.debug("[%s]: DockerAPI init()", self._instance)

        # Get URL
        url: str = self._config[CONF_URL]

        # HA sometimes makes the url="", which aiodocker does not like
        if url is not None and url == "":
            url = None

        # A Unix connection should contain 'unix://' in the URL
        unixConnection = url is not None and url.find("unix://") == 0

        # If it is not a Unix connection, it should be a TCP connection
        tcpConnection = url is not None and not unixConnection

        if unixConnection:
            _LOGGER.debug("[%s]: Docker URL contains a Unix socket connection: '%s'", self._instance, url)

            # Try to fix unix:// to unix:/// (3 are required by aiodocker)
            if url.find("unix:///") == -1:
                url = url.replace("unix://", "unix:///")

        elif tcpConnection:
            _LOGGER.debug("[%s]: Docker URL contains a TCP connection: '%s'", self._instance, url)

            # When we reconnect with tcp, we should delay - docker is maybe not fully ready
            if startCount > 0:
                await asyncio.sleep(5)

        else:
            _LOGGER.debug(
                "[%s]: Docker URL is auto-detect (most likely using 'unix://var/run/docker.socket')",
                self._instance,
            )



        # Remove Docker environment variables
        os.environ.pop("DOCKER_TLS_VERIFY", None)
        os.environ.pop("DOCKER_CERT_PATH", None)

        # Setup Docker parameters
        self._tcp_connector = None
        self._tcp_session = None
        self._tcp_ssl_context = None

        # If is a TCP connection, then do check TCP/SSL
        if tcpConnection:
            # Check if URL is valid
            if not (
                url.find("tcp:") == 0
                or url.find("http:") == 0
                or url.find("https:") == 0
            ):
                raise ValueError(
                    f"[{self._instance}] Docker URL '{url}' does not start with tcp:, http: or https:"
                )

            if self._config[CONF_CERTPATH] and url.find("http:") == 0:
                # fixup URL and warn
                _LOGGER.warning(
                    "[%s] Docker URL '%s' should be https instead of http when using certificate path",
                    self._instance,
                    url,
                )
                url = url.replace("http:", "https:")

            if self._config[CONF_CERTPATH] and url.find("tcp:") == 0:
                # fixup URL and warn
                _LOGGER.warning(
                    "[%s] Docker URL '%s' should be https instead of tcp when using certificate path",
                    self._instance,
                    url,
                )
                url = url.replace("tcp:", "https:")

            if self._config[CONF_CERTPATH]:
                _LOGGER.debug(
                    "[%s]: Docker certification path is '%s' SSL/TLS will be used",
                    self._instance,
                    self._config[CONF_CERTPATH],
                )

                # Create our SSL context object
                self._tcp_ssl_context = await self._hass.async_add_executor_job(
                    self._docker_ssl_context
                )

            # Setup new TCP connection, otherwise timeout takes toooo long
            self._tcp_connector = TCPConnector(ssl=self._tcp_ssl_context)
            self._tcp_session = ClientSession(
                connector=self._tcp_connector,
                timeout=ClientTimeout(
                    connect=5,
                    sock_connect=5,
                    total=10,
                ),
            )

        try:
            # Initiate the aiodocker instance now. Could raise an exception
            self._api = aiodocker.Docker(
                url=url,
                connector=self._tcp_connector,
                session=self._tcp_session,
                ssl_context=self._tcp_ssl_context,
            )

            versionInfo = await self._api.version()
            version: str | None = versionInfo.get("Version", None)

            # Pre 19.03 support memory calculation is dropped
            _LOGGER.debug("[%s]: Docker version: %s", self._instance, version)
        except aiodocker.exceptions.DockerError as err:
            _LOGGER.error(
                "[%s]: Docker API connection failed: %s", self._instance, str(err)
            )
            raise ConfigEntryAuthFailed from err
        except Exception:
            raise

        # Get the list of containers to monitor
        containers = await self._api.containers.list(all=True)

        # We only store names, we do not initialize them. This happens in run()
        for container in containers or []:
            # Determine name from Docker API, it contains an array with a slash
            cname: str = container._container["Names"][0][1:]

            # Add container name to the list
            self._containers[cname] = None

    #############################################################
    async def run(self):

        _LOGGER.debug("[%s]: DockerAPI run()", self._instance)

        # Start task to monitor events of create/delete/start/stop
        if "events" not in self._tasks:
            self._tasks["events"] = asyncio.create_task(self._run_docker_events())

        # Start task to monitor total/running containers
        if "info" not in self._tasks:
            self._tasks["info"] = asyncio.create_task(self._run_docker_info())

        # Loop through containers and do it
        for cname in self._containers:

            # Skip already initialized containers
            if self._containers[cname]:
                continue

            # We will monitor all containers, including excluded ones.
            # This is needed to get total CPU/Memory usage.

            _LOGGER.debug("[%s] %s: Container monitored", self._instance, cname)

            # Create our Docker Container API
            self._containers[cname] = DockerContainerAPI(
                self._config,
                self._api,
                cname,
            )
            await self._containers[cname].init()

        self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._monitor_stop)

    #############################################################
    async def load(self):

        _LOGGER.debug("[%s]: DockerAPI load()", self._instance)

        for component in COMPONENTS:
            load_platform(
                self._hass,
                component,
                DOMAIN,
                {CONF_NAME: self._instance},
                self._config,
            )

    #############################################################
    async def destroy(self) -> None:
        """Destroy the DockerAPI and its containers."""

        # Cancel all main tasks
        for key in self._tasks:
            try:
                _LOGGER.debug("[%s]: Cancelling task '%s'", self._instance, key)
                result = self._tasks[key].cancel()
                _LOGGER.debug(
                    "[%s]: Cancelled task '%s' result=%s", self._instance, key, result
                )
            except Exception as err:
                _LOGGER.error(
                    "[%s]: Cancelling task '%s' FAILED '%s'",
                    self._instance,
                    key,
                    str(err),
                )
                pass

        # Cancel the containers

        for container in self._containers.values():
            _LOGGER.debug(
                "[%s] %s: Container cancelled", self._instance, container._name
            )
            await container.destroy()
            # TBD clear container from list?

        # Close session if initialized
        if self._tcp_session:
            self._tcp_session.detach()

        # Clear api value
        # self._api = None

    #############################################################
    def _docker_ssl_context(self) -> ssl.SSLContext | None:
        """
        Create a SSLContext object
        """

        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        context.set_ciphers(ssl._RESTRICTED_SERVER_CIPHERS)  # type: ignore

        path2 = Path(self._config[CONF_CERTPATH])

        context.load_verify_locations(cafile=str(path2 / "ca.pem"))
        context.load_cert_chain(
            certfile=str(path2 / "cert.pem"), keyfile=str(path2 / "key.pem")
        )

        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        context.check_hostname = False

        return context

    #############################################################
    def _monitor_stop(self, _service_or_event: Event) -> None:
        """Stop the monitor thread."""

        _LOGGER.info("[%s]: Stopping Monitor Docker thread", self._instance)

    #############################################################
    async def _reconnectx(self):
        while True:
            _LOGGER.debug("[%s] Reconnecting", self._instance)

            try:
                await self.init()
                break
            except Exception as err:
                _LOGGER.error(
                    "[%s] Failed Docker connect (%s). Retry in %d seconds",
                    self._instance,
                    str(err),
                    self._retry_interval,
                )
                await asyncio.sleep(self._retry_interval)

        _LOGGER.debug("[%s] Reconnect success", self._instance)

    #############################################################
    def remove_entities(self) -> None:
        """Remove docker info entities."""

        if len(self._subscribers) > 0:
            _LOGGER.debug(
                "[%s]: Removing entities from Docker info",
                self._instance,
            )

        for callback in self._subscribers:
            callback(remove=True)

        self._subscriber: list[Callable] = []

    #############################################################
    def register_callback(self, callback: Callable, variable: str) -> None:
        """Register callback from sensor."""
        if callback not in self._subscribers:
            _LOGGER.debug("[%s]: Added callback entity: %s", self._instance, variable)
            self._subscribers.append(callback)

    #############################################################
    async def _run_docker_events(self) -> None:
        """Function to retrieve docker events. We can add or remove monitored containers."""

        try:
            subscriber = self._api.events.subscribe()

            while True:
                event: dict = await subscriber.get()

                # Dump all raw events
                if event is None:
                    _LOGGER.debug("[%s] run_docker_events RAW: None", self._instance)
                else:
                    # If Type=container, give some additional information
                    addlog = ""
                    if event["Type"] == "container":
                        try:
                            addlog = f", Name={event['Actor']['Attributes']['name']}"
                        except:
                            pass

                    _LOGGER.debug(
                        "[%s] run_docker_events Type=%s%s, Action=%s",
                        self._instance,
                        event["Type"],
                        addlog,
                        event["Action"],
                    )

                # When we receive none, the connection normally is broken
                if event is None:
                    _LOGGER.error("[%s]: run_docker_events loop ended", self._instance)

                    # Set this to know if we stopped or HASS is stopping
                    self._dockerStopped = True

                    # Remove the docker info sensors
                    self.remove_entities()

                    # Remove all the sensors/switches/buttons, they will be auto created if connection is working again
                    for cname in list(self._containers.keys()):
                        try:
                            await self._container_remove(cname)
                        except Exception as err:
                            exc_info = True if str(err) == "" else False
                            _LOGGER.error(
                                "[%s]: Stopping gave an error %s",
                                self._instance,
                                str(err),
                                exc_info=exc_info,
                            )

                    # Stop everything and return to the main thread
                    self._monitor_stop(self._config[CONF_NAME])

                    # TODO: improve reconnectx
                    await self._reconnectx()

                    break

                # Only monitor container events
                if event["Type"] == CONTAINER:
                    if event["Action"] == "create":
                        # Check if another task is running, ifso, we don't create a new one
                        taskcreated = (
                            True if self._event_create or self._event_destroy else False
                        )

                        cname = event["Actor"]["Attributes"]["name"]

                        # Add container name to containers to be monitored this has to
                        # be a new task, otherwise it will block our event monitoring
                        if cname not in self._event_create:
                            _LOGGER.debug(
                                "[%s] %s: Event create container", self._instance, cname
                            )
                            self._event_create[cname] = 0
                        else:
                            _LOGGER.error(
                                "[%s] %s: Event create container, but already in working table?",
                                self._instance,
                                cname,
                            )

                        if self._event_create and not taskcreated:
                            await self._container_create_destroy()

                    elif event["Action"] == "destroy":
                        # Check if another task is running, ifso, we don't create a new one
                        taskcreated = (
                            True if self._event_create or self._event_destroy else False
                        )

                        cname = event["Actor"]["Attributes"]["name"]

                        # Remove container name to containers to be monitored this has to
                        # be a new task, otherwise it will block our event monitoring
                        if cname in self._event_create:
                            _LOGGER.warning(
                                "[%s] %s: Event destroy received, but create wasn't executed yet",
                                self._instance,
                                cname,
                            )
                            del self._event_create[cname]
                        elif cname not in self._event_destroy:
                            _LOGGER.debug(
                                "[%s] %s: Event destroy container",
                                self._instance,
                                cname,
                            )
                            self._event_destroy[cname] = 0
                        else:
                            _LOGGER.error(
                                "%s: Event destroy container, but already in working table?",
                                cname,
                            )

                        if self._event_destroy and not taskcreated:
                            await self._container_create_destroy()
                    elif event["Action"] == "rename":
                        # during a docker-compose up -d <container> the old container can be renamed
                        # sensors/switch/button should be removed before the new container is monitored

                        # New name
                        cname = event["Actor"]["Attributes"]["name"]

                        # Old name, and remove leading slash
                        oname = event["Actor"]["Attributes"]["oldName"]
                        oname = oname[1:]

                        if oname in self._containers:
                            _LOGGER.debug(
                                "[%s] %s: Event rename container to '%s'",
                                self._instance,
                                oname,
                                cname,
                            )

                            # First remove the newly create container, has a temporary name
                            if oname in self._event_create:
                                _LOGGER.warning(
                                    "[%s] %s: Event rename received, but create wasn't executed yet",
                                    self._instance,
                                    oname,
                                )
                                del self._event_create[cname]
                            elif oname not in self._event_destroy:
                                _LOGGER.debug(
                                    "[%s] %s: Event rename (destroy) container",
                                    self._instance,
                                    oname,
                                )
                                self._event_destroy[oname] = 0
                            else:
                                _LOGGER.error(
                                    "%s: Event rename (destroy) container, but already in working table?",
                                    oname,
                                )

                            # taskcreate is not unavailable, so remove for now
                            if self._event_destroy:
                                await self._container_create_destroy()

                            # Second re-add the container with the original name
                            taskcreated = (
                                True
                                if self._event_create or self._event_destroy
                                else False
                            )

                            if cname not in self._event_create:
                                _LOGGER.debug(
                                    "[%s] %s: Event rename (create) container",
                                    self._instance,
                                    cname,
                                )
                                self._event_create[cname] = 0
                            else:
                                _LOGGER.error(
                                    "[%s] %s: Event rename (create) container, but already in working table?",
                                    self._instance,
                                    cname,
                                )

                            if self._event_create and not taskcreated:
                                await self._container_create_destroy()
                        else:
                            _LOGGER.error(
                                "[%s] %s: Event rename container doesn't exist in list?",
                                self._instance,
                                oname,
                            )

        except Exception as err:
            exc_info = True if str(err) == "" else False
            _LOGGER.error(
                "[%s]: run_docker_events (%s)",
                self._instance,
                str(err),
                exc_info=exc_info,
            )

    #############################################################
    async def _container_create_destroy(self) -> None:
        """Handles create or destroy of container events."""

        try:
            while self._event_create or self._event_destroy:
                # Go through create loop first
                for cname in self._event_create:
                    if self._event_create[cname] > 2:
                        del self._event_create[cname]
                        await self._container_add(cname)
                        break
                    else:
                        self._event_create[cname] += 1
                else:
                    # If all create, we can handle the destroy loop
                    for cname in self._event_destroy:
                        await self._container_remove(cname)

                    self._event_destroy = {}

                # Sleep for 1 second, don't try to create it too fast
                await asyncio.sleep(1)

        except Exception as err:
            exc_info = True if str(err) == "" else False
            _LOGGER.error(
                "[%s]: container_create_destroy (%s)",
                self._instance,
                str(err),
                exc_info=exc_info,
            )

    #############################################################
    async def _container_add(self, cname: str) -> None:
        if cname in self._containers:
            _LOGGER.error("[%s] %s: Container already monitored", self._instance, cname)
            return

        _LOGGER.debug("[%s] %s: Starting Container Monitor", self._instance, cname)

        # Create our Docker Container API
        self._containers[cname] = DockerContainerAPI(
            self._config, self._api, cname, atInit=False
        )

        # We should wait until container is attached
        result = await self._containers[cname]._initGetContainer()

        if result:
            # Lets wait 1 second before we try to create sensors/switches/buttons
            await asyncio.sleep(1)

            for component in COMPONENTS:
                load_platform(
                    self._hass,
                    component,
                    DOMAIN,
                    {CONF_NAME: self._instance, CONTAINER: cname},
                    self._config,
                )
        else:
            _LOGGER.error(
                "[%s] %s: Problem during start of monitoring", self._instance, cname
            )

    #############################################################
    async def _container_remove(self, cname: str) -> None:
        if cname in self._containers:
            _LOGGER.debug("[%s] %s: Stopping Container Monitor", self._instance, cname)
            self._containers[cname].cancel_task()
            self._containers[cname].remove_entities()
            await asyncio.sleep(0.1)
            del self._containers[cname]
        else:
            _LOGGER.error("[%s] %s: Container is NOT monitored", self._instance, cname)

    #############################################################
    async def _run_docker_info(self) -> None:
        """Function to retrieve information like docker info."""

        loopInit = False
        self._dockerStopped = False

        while True:
            error = True

            try:
                if self._dockerStopped:
                    _LOGGER.debug("[%s]: Stopping docker info thread", self._instance)
                    break

                info = await self._api.system.info()
                self._info[DOCKER_INFO_VERSION] = info.get("ServerVersion")
                self._info[DOCKER_INFO_CONTAINER_RUNNING] = info.get(
                    "ContainersRunning"
                )
                self._info[DOCKER_INFO_CONTAINER_PAUSED] = info.get("ContainersPaused")
                self._info[DOCKER_INFO_CONTAINER_STOPPED] = info.get(
                    "ContainersStopped"
                )
                self._info[DOCKER_INFO_CONTAINER_TOTAL] = info.get("Containers")
                self._info[DOCKER_INFO_IMAGES] = info.get("Images")

                self._info[ATTR_MEMORY_LIMIT] = info.get("MemTotal")
                self._info[ATTR_ONLINE_CPUS] = info.get("NCPU")
                self._info[ATTR_VERSION_OS] = info.get("OperatingSystem")
                self._info[ATTR_VERSION_OS_TYPE] = info.get("OSType")
                self._info[ATTR_VERSION_ARCH] = info.get("Architecture")
                self._info[ATTR_VERSION_KERNEL] = info.get("KernelVersion")

                self._info[DOCKER_STATS_CPU_PERCENTAGE] = 0.0
                self._info[DOCKER_STATS_1CPU_PERCENTAGE] = 0.0
                self._info[DOCKER_STATS_MEMORY] = 0
                self._info[DOCKER_STATS_MEMORY_PERCENTAGE] = 0.0

                # Now go through all containers and get the cpu/memory stats
                for container in self._containers.values():
                    if container is None:
                        _LOGGER.warning(
                            "[%s]: run_docker_info container is not yet initilized",
                            self._instance,
                        )
                    try:
                        info = container.get_info()
                        if info.get(CONTAINER_INFO_STATE) == "running":
                            stats = container.get_stats()
                            if stats.get(CONTAINER_STATS_CPU_PERCENTAGE) is not None:
                                self._info[DOCKER_STATS_CPU_PERCENTAGE] += stats.get(
                                    CONTAINER_STATS_CPU_PERCENTAGE
                                )
                            if stats.get(CONTAINER_STATS_MEMORY) is not None:
                                self._info[DOCKER_STATS_MEMORY] += stats.get(
                                    CONTAINER_STATS_MEMORY
                                )
                    except Exception as err:
                        exc_info = True if str(err) == "" else False
                        _LOGGER.error(
                            "[%s]: run_docker_info memory/cpu of X (%s)",
                            self._instance,
                            str(err),
                            exc_info=exc_info,
                        )

                # Calculate memory percentage
                if (
                    self._info[ATTR_MEMORY_LIMIT] is not None
                    and self._info[ATTR_MEMORY_LIMIT] != 0
                ):
                    self._info[DOCKER_STATS_MEMORY_PERCENTAGE] = round(
                        self._info[DOCKER_STATS_MEMORY]
                        / toMB(self._info[ATTR_MEMORY_LIMIT], 4)
                        * 100,
                        self._config[CONF_PRECISION_MEMORY_PERCENTAGE],
                    )

                # Try to fix possible 0 values in history at start-up
                if loopInit:
                    self._info[DOCKER_STATS_CPU_PERCENTAGE] = round(
                        self._info[DOCKER_STATS_CPU_PERCENTAGE],
                        self._config[CONF_PRECISION_CPU],
                    )

                    # Calculate for 0-100%
                    if self._info[DOCKER_STATS_CPU_PERCENTAGE] is None:
                        self._info[DOCKER_STATS_1CPU_PERCENTAGE] = None
                    else:
                        self._info[DOCKER_STATS_1CPU_PERCENTAGE] = round(
                            (
                                self._info[DOCKER_STATS_CPU_PERCENTAGE]
                                / self._info[ATTR_ONLINE_CPUS]
                            ),
                            self._config[CONF_PRECISION_CPU],
                        )

                    self._info[DOCKER_STATS_MEMORY] = round(
                        self._info[DOCKER_STATS_MEMORY],
                        self._config[CONF_PRECISION_MEMORY_MB],
                    )

                    self._info[DOCKER_STATS_MEMORY_PERCENTAGE] = round(
                        self._info[DOCKER_STATS_MEMORY_PERCENTAGE],
                        self._config[CONF_PRECISION_MEMORY_PERCENTAGE],
                    )
                else:
                    self._info[DOCKER_STATS_CPU_PERCENTAGE] = (
                        None
                        if self._info[DOCKER_STATS_CPU_PERCENTAGE] == 0.0
                        else round(
                            self._info[DOCKER_STATS_CPU_PERCENTAGE],
                            self._config[CONF_PRECISION_CPU],
                        )
                    )

                    # Calculate for 0-100%
                    if self._info[DOCKER_STATS_CPU_PERCENTAGE] == 0.0:
                        self._info[DOCKER_STATS_1CPU_PERCENTAGE] = None
                    elif self._info[DOCKER_STATS_CPU_PERCENTAGE] is None:
                        self._info[DOCKER_STATS_1CPU_PERCENTAGE] = None
                    else:
                        self._info[DOCKER_STATS_1CPU_PERCENTAGE] = round(
                            (
                                self._info[DOCKER_STATS_CPU_PERCENTAGE]
                                / self._info[ATTR_ONLINE_CPUS]
                            ),
                            self._config[CONF_PRECISION_CPU],
                        )

                    self._info[DOCKER_STATS_MEMORY] = (
                        None
                        if self._info[DOCKER_STATS_MEMORY] == 0.0
                        else round(
                            self._info[DOCKER_STATS_MEMORY],
                            self._config[CONF_PRECISION_MEMORY_MB],
                        )
                    )

                    self._info[DOCKER_STATS_MEMORY_PERCENTAGE] = (
                        None
                        if self._info[DOCKER_STATS_MEMORY_PERCENTAGE] == 0.0
                        else round(
                            self._info[DOCKER_STATS_MEMORY_PERCENTAGE],
                            self._config[CONF_PRECISION_MEMORY_PERCENTAGE],
                        )
                    )

                _LOGGER.debug(
                    "[%s]: Version: %s, Containers: %s, Running: %s, CPU: %s%%, 1CPU: %s%%, Memory: %sMB, %s%%",
                    self._instance,
                    self._info[DOCKER_INFO_VERSION],
                    self._info[DOCKER_INFO_CONTAINER_TOTAL],
                    self._info[DOCKER_INFO_CONTAINER_RUNNING],
                    self._info[DOCKER_STATS_CPU_PERCENTAGE],
                    self._info[DOCKER_STATS_1CPU_PERCENTAGE],
                    self._info[DOCKER_STATS_MEMORY],
                    self._info[DOCKER_STATS_MEMORY_PERCENTAGE],
                )

                loopInit = True
                error = False

            except asyncio.TimeoutError as err:
                _LOGGER.error(
                    "[%s]: run_docker_info (%s) TCP Timeout. Retry in %d seconds",
                    self._instance,
                    self._retry_interval,
                )
            except Exception as err:
                exc_info = True if str(err) == "" else False
                _LOGGER.error(
                    "[%s]: run_docker_info (%s). Retry in %d seconds",
                    self._instance,
                    str(err),
                    self._retry_interval,
                    exc_info=exc_info,
                )

            if error:
                await asyncio.sleep(self._retry_interval)
            else:
                await asyncio.sleep(self._interval)

    #############################################################
    def list_containers(self):
        return self._containers.keys()

    #############################################################
    def get_container(self, cname: str) -> "DockerContainerAPI":
        if cname in self._containers:
            return self._containers[cname]
        else:
            _LOGGER.error(
                "[%s]: Trying to get a not existing container %s", self._instance, cname
            )
            return None

    #############################################################
    def get_info(self) -> dict[str, Any]:
        return self._info

    #############################################################
    def get_url(self) -> str:
        return self._config[CONF_URL]


#################################################################
class DockerContainerAPI:
    """Docker Container API abstraction."""

    def __init__(
        self,
        config: ConfigType,
        api: aiodocker.Docker,
        cname: str,
        atInit=True,
    ):
        self._config = config
        self._api = api
        self._instance: str = config[CONF_NAME]
        self._memChange: int = config[CONF_MEMORYCHANGE]
        self._name = cname
        self._interval: int = config[CONF_SCAN_INTERVAL]
        self._retry_interval: int = config[CONF_RETRY]
        self._busy = False
        self._atInit = atInit
        self._task: asyncio.Task | None = None
        self._subscribers: list[Callable] = []
        self._cpu_old: dict[str, int] = {}
        self._network_old: dict[str, int | datetime] = {}
        self._network_error = 0
        self._memory_error = 0
        self._cpu_error = 0
        self._memory_prev: float | None = None
        self._memory_prev_breach = False
        self._memory_percent_prev: float | None = None
        self._memory_percent_prev_breach = False

        self._info: dict[str, Any] = {}
        self._stats: dict[str, Any] = {}

    async def init(self):
        # During start-up we will wait on container attachment,
        # preventing concurrency issues the main HA loop (we are
        # othside that one with our threads)

        _LOGGER.debug("[%s] %s: DockerContainerAPI init()", self._instance, self._name)

        if self._atInit:
            try:
                self._container = await self._api.containers.get(self._name)
            except Exception as err:
                exc_info = True if str(err) == "" else False
                _LOGGER.error(
                    "[%s] %s: Container not available anymore (1) (%s)",
                    self._instance,
                    self._name,
                    str(err),
                    exc_info=exc_info,
                )
                return  # Could be necessary to do something more here

            self._task = asyncio.create_task(self._run())

    #############################################################
    async def _initGetContainer(self) -> bool:
        # If we noticed a event=create, we need to attach here.
        # The run_until_complete doesn't work, because we are already
        # in a running loop.

        try:
            self._container = await self._api.containers.get(self._name)
        except aiodocker.exceptions.DockerError as err:
            _LOGGER.error(
                "[%s] %s: Container not available anymore (2a) (%s)",
                self._instance,
                self._name,
                str(err),
            )
            return False
        except Exception as err:
            exc_info = True if str(err) == "" else False
            _LOGGER.error(
                "[%s] %s: Container not available anymore (2b) (%s)",
                self._instance,
                self._name,
                str(err),
                exc_info=exc_info,
            )
            return False

        self._task = asyncio.create_task(self._run())

        return True

    #############################################################
    async def destroy(self) -> None:

        if self._task:
            try:
                _LOGGER.debug("[%s] %s: Cancelling task", self._instance, self._name)
                result = self._task.cancel()
                _LOGGER.debug(
                    "[%s] %s: Cancelled task result=%s",
                    self._instance,
                    self._name,
                    result,
                )
            except Exception as err:
                _LOGGER.error(
                    "[%s] %s: Cancelling task FAILED '%s'",
                    self._instance,
                    self._name,
                    str(err),
                )
                pass
        else:
            _LOGGER.error("[%s] %s: No task to cancel", self._instance, self._name)

    #############################################################
    async def _run(self) -> None:
        """Loop to gather container info/stats."""

        while True:
            sendNotify = True
            error = True

            try:
                # Don't check container if we are doing a start/stop
                if not self._busy:
                    await self._run_container_info()

                    # Only run stats if container is running
                    if self._info[CONTAINER_INFO_STATE] in ("running", "paused"):
                        await self._run_container_stats()
                else:
                    _LOGGER.debug(
                        "[%s] %s: Waiting on stop/start of container",
                        self._instance,
                        self._name,
                    )
                    sendNotify = False

                # No error, so normal interval
                error = False

            except concurrent.futures._base.CancelledError:
                _LOGGER.debug(
                    "[%s] %s: Container received concurrent.futures._base.CancelledError",
                    self._instance,
                    self._name,
                )
                pass
                break
            except aiodocker.exceptions.DockerError as err:
                _LOGGER.error(
                    "[%s] %s: Container not available anymore (3a) (%s). Retry in %d seconds",
                    self._instance,
                    self._name,
                    str(err),
                    self._retry_interval,
                )
            except asyncio.exceptions.CancelledError as err:
                _LOGGER.error(
                    "[%s] %s: Container not available anymore (3c) CancelledError. Retry in %d seconds",
                    self._instance,
                    self._name,
                    self._retry_interval,
                )
            except asyncio.TimeoutError as err:
                _LOGGER.error(
                    "[%s] %s: Container not available anymore (3d) TimeoutError. Retry in %d seconds",
                    self._instance,
                    self._name,
                    self._retry_interval,
                )
            except Exception as err:
                exc_info = True if str(err) == "" else False
                _LOGGER.error(
                    "[%s] %s: Container not available anymore (3b) (%s). Retry in %d seconds",
                    self._instance,
                    self._name,
                    str(err),
                    self._retry_interval,
                    exc_info=exc_info,
                )

            # Send values to sensors/switch
            if sendNotify:
                self._notify()

            # TODO: on error, increase sleep

            # Sleep in normal and exception situation
            if error:
                await asyncio.sleep(self._retry_interval)
            else:
                await asyncio.sleep(self._interval)

    #############################################################
    async def _run_container_info(self) -> None:
        """Get container information, but we can not get
        the uptime of this container, that is only available
        while listing all containers :-(.
        """

        self._info = {}

        raw: dict = await self._container.show()

        self._info[CONTAINER_INFO_STATE] = raw["State"]["Status"]
        self._info[CONTAINER_INFO_IMAGE] = raw["Config"]["Image"]
        self._info[CONTAINER_INFO_IMAGE_HASH] = raw["Image"]

        if self._network_error <= 5:
            if CONTAINER_INFO_NETWORK_AVAILABLE not in self._info:
                self._info[CONTAINER_INFO_NETWORK_AVAILABLE] = (
                    False
                    if raw["HostConfig"]["NetworkMode"] in ["host", "none"]
                    else True
                )
        else:
            self._info[CONTAINER_INFO_NETWORK_AVAILABLE] = False

        try:
            self._info[CONTAINER_INFO_HEALTH] = raw["State"]["Health"]["Status"]
        except:
            self._info[CONTAINER_INFO_HEALTH] = "unknown"

        # We only do a calculation of startedAt, because we use it twice
        startedAt = parser.parse(raw["State"]["StartedAt"])

        # Determine the container status in the format:
        # Up 6 days
        # Up 6 days (Paused)
        # Exited (0) 2 months ago
        # Restarting (99) 5 seconds ago

        if self._info[CONTAINER_INFO_STATE] == "running":
            self._info[CONTAINER_INFO_STATUS] = "Up {}".format(
                self._calcdockerformat(startedAt)
            )
        elif self._info[CONTAINER_INFO_STATE] == "exited":
            self._info[CONTAINER_INFO_STATUS] = "Exited ({}) {} ago".format(
                raw["State"]["ExitCode"],
                self._calcdockerformat(parser.parse(raw["State"]["FinishedAt"])),
            )
        elif self._info[CONTAINER_INFO_STATE] == "created":
            self._info[CONTAINER_INFO_STATUS] = "Created {} ago".format(
                self._calcdockerformat(parser.parse(raw["Created"]))
            )
        elif self._info[CONTAINER_INFO_STATE] == "restarting":
            self._info[CONTAINER_INFO_STATUS] = "Restarting"
        elif self._info[CONTAINER_INFO_STATE] == "paused":
            self._info[CONTAINER_INFO_STATUS] = "Up {} (Paused)".format(
                self._calcdockerformat(startedAt)
            )
        else:
            self._info[CONTAINER_INFO_STATUS] = "None ({})".format(
                raw["State"]["Status"]
            )

        if self._info[CONTAINER_INFO_STATE] in ("running", "paused"):
            self._info[CONTAINER_INFO_UPTIME] = dt_util.as_local(startedAt).isoformat()
        else:
            self._info[CONTAINER_INFO_UPTIME] = None
            _LOGGER.debug(
                "[%s] %s: %s",
                self._instance,
                self._name,
                self._info[CONTAINER_INFO_STATUS],
            )

    #############################################################
    async def _run_container_stats(self) -> None:
        # Initialize stats information
        stats: dict[str, Any] = {}
        stats["cpu"] = {}
        stats["memory"] = {}
        stats["network"] = {}
        stats["read"] = {}

        # Get container stats, only interested in [0]
        rawarr = await self._container.stats(stream=False)

        # Could be out-of-range when stopping/renaming
        try:
            raw: dict[str, Any] = rawarr[0]
        except IndexError:
            return

        stats["read"] = parser.parse(raw["read"])

        # Gather CPU information
        cpu_stats = {}
        try:
            cpu_new = {}
            cpu_new["total"] = raw["cpu_stats"]["cpu_usage"]["total_usage"]
            cpu_new["system"] = raw["cpu_stats"]["system_cpu_usage"]

            # Compatibility wih older Docker API
            if "online_cpus" in raw["cpu_stats"]:
                cpu_stats["online_cpus"] = raw["cpu_stats"]["online_cpus"]
            else:
                cpu_stats["online_cpus"] = len(
                    raw["cpu_stats"]["cpu_usage"]["percpu_usage"] or []
                )

            # Calculate cpu usage, but first iteration we don't know it
            if self._cpu_old:
                cpu_delta = float(cpu_new["total"] - self._cpu_old["total"])
                system_delta = float(cpu_new["system"] - self._cpu_old["system"])

                cpu_stats["total"] = round(0.0, PRECISION)
                if cpu_delta > 0.0 and system_delta > 0.0:
                    cpu_stats["total"] = round(
                        (cpu_delta / system_delta)
                        * float(cpu_stats["online_cpus"])
                        * 100.0,
                        self._config[CONF_PRECISION_CPU],
                    )

            self._cpu_old = cpu_new

            if self._cpu_error > 0:
                _LOGGER.debug(
                    "[%s] %s: CPU error count %s reset to 0",
                    self._instance,
                    self._name,
                    self._cpu_error,
                )

            self._cpu_error = 0

        except KeyError as err:
            # Something wrong with the raw data
            if self._cpu_error == 0:
                _LOGGER.error(
                    "[%s] %s: Cannot determine CPU usage for container (%s)",
                    self._instance,
                    self._name,
                    str(err),
                )
                if "cpu_stats" in raw:
                    _LOGGER.error(
                        "[%s] %s: Raw 'cpu_stats' %s", self._name, raw["cpu_stats"]
                    )
                else:
                    _LOGGER.error(
                        "[%s] %s: No 'cpu_stats' found in raw packet",
                        self._instance,
                        self._name,
                    )

            self._cpu_error += 1

        # Gather memory information
        memory_stats: dict[str, float | None] = {}

        try:
            memory_stats["usage"] = None

            cache = 0
            # https://docs.docker.com/engine/reference/commandline/stats/
            # Version is 19.04 or higher, don't use "cache"
            if "total_inactive_file" in raw["memory_stats"]["stats"]:
                cache = raw["memory_stats"]["stats"]["total_inactive_file"]
            elif "inactive_file" in raw["memory_stats"]["stats"]:
                cache = raw["memory_stats"]["stats"]["inactive_file"]

            memory_stats["usage"] = toMB(
                raw["memory_stats"]["usage"] - cache,
                self._config[CONF_PRECISION_MEMORY_MB],
            )
            memory_stats["limit"] = toMB(
                raw["memory_stats"]["limit"], self._config[CONF_PRECISION_MEMORY_MB]
            )
            memory_stats["usage_percent"] = round(
                float(memory_stats["usage"]) / float(memory_stats["limit"]) * 100.0,
                self._config[CONF_PRECISION_MEMORY_PERCENTAGE],
            )

            if self._memory_error > 0:
                _LOGGER.debug(
                    "[%s] %s: Memory error count %s reset to 0",
                    self._instance,
                    self._name,
                    self._memory_error,
                )

            self._memory_error = 0

        except (KeyError, TypeError) as err:
            if self._memory_error == 0:
                _LOGGER.error(
                    "[%s] %s: Cannot determine memory usage for container (%s)",
                    self._instance,
                    self._name,
                    str(err),
                )
                if "memory_stats" in raw:
                    _LOGGER.error(
                        "[%s] %s: Raw 'memory_stats' %s",
                        self._instance,
                        self._name,
                        raw["memory_stats"],
                    )
                else:
                    _LOGGER.error(
                        "[%s] %s: No 'memory_stats' found in raw packet",
                        self._instance,
                        self._name,
                    )

            self._memory_error += 1

        _LOGGER.debug(
            "[%s] %s: CPU: %s%%, Memory: %sMB, %s%%",
            self._instance,
            self._name,
            cpu_stats.get("total", None),
            memory_stats.get("usage", None),
            memory_stats.get("usage_percent", None),
        )

        # Default value
        mem_breach = False

        # Try to figure out if we should report the memory value or not
        if (
            memory_stats.get("usage", None)
            and self._memory_prev
            and not self._memory_prev_breach
        ):
            mem_diff = abs((memory_stats["usage"] / self._memory_prev) - 1) * 100

            if self._memChange < 100 and mem_diff >= self._memChange:
                mem_breach = True

            _LOGGER.debug(
                "[%s] %s: Mem Diff: %s%%, Curr: %s, Prev: %s, Breach: %s",
                self._instance,
                self._name,
                round(mem_diff, 3),
                memory_stats.get("usage", None),
                self._memory_prev,
                mem_breach,
            )

        else:
            self._memory_prev_breach = False

        """
        self._memory_prev = None
        self._memory_prev_breach = False
        self._memory_percent_prev = None
        self._memory_percent_prev_breach = False
        """

        # Check if we should block the current value or not
        if mem_breach and not self._memory_prev_breach:
            _LOGGER.debug(
                "[%s] %s: Memory breach %s%%", self._instance, self._name, mem_breach
            )

            # Store values into previous
            tmp1 = self._memory_prev
            tmp2 = self._memory_percent_prev
            self._memory_prev = memory_stats.get("usage", None)
            self._memory_prev_breach = mem_breach
            self._memory_percent_prev = memory_stats.get("usage_percent", None)
            memory_stats["usage"] = tmp1
            memory_stats["usage_percent"] = tmp2
        else:
            # Store values into previous
            self._memory_prev = memory_stats.get("usage", None)
            self._memory_prev_breach = mem_breach
            self._memory_percent_prev = memory_stats.get("usage_percent", None)

        # Gather network information, doesn't work in network=host mode
        network_stats: dict[str, int | float] = {}
        if self._info[CONTAINER_INFO_NETWORK_AVAILABLE]:
            try:
                network_new = {}
                network_stats["total_tx"] = 0
                network_stats["total_rx"] = 0
                for if_name, data in raw["networks"].items():
                    network_stats["total_tx"] += data["tx_bytes"]
                    network_stats["total_rx"] += data["rx_bytes"]

                network_new = {
                    "read": stats["read"],
                    "total_tx": network_stats["total_tx"],
                    "total_rx": network_stats["total_rx"],
                }

                if self._network_old:
                    tx = network_new["total_tx"] - self._network_old["total_tx"]
                    rx = network_new["total_rx"] - self._network_old["total_rx"]
                    tim = (
                        network_new["read"] - self._network_old["read"]
                    ).total_seconds()

                    # Calculate speed, also convert to kByte/sec
                    network_stats["speed_tx"] = toKB(
                        float(tx) / tim, self._config[CONF_PRECISION_NETWORK_KB]
                    )
                    network_stats["speed_rx"] = toKB(
                        float(rx) / tim, self._config[CONF_PRECISION_NETWORK_KB]
                    )

                self._network_old = network_new

                # Convert total to MB
                network_stats["total_tx"] = toMB(
                    network_stats["total_tx"], self._config[CONF_PRECISION_NETWORK_MB]
                )
                network_stats["total_rx"] = toMB(
                    network_stats["total_rx"], self._config[CONF_PRECISION_NETWORK_MB]
                )

            except KeyError as err:
                _LOGGER.error(
                    "[%s] %s: Can not determine network usage for container (%s)",
                    self._instance,
                    self._name,
                    str(err),
                )
                if "networks" in raw:
                    _LOGGER.error(
                        "[%s] %s: Raw 'networks' %s",
                        raw["networks"],
                        self._instance,
                        self._name,
                    )
                else:
                    _LOGGER.error(
                        "[%s] %s: No 'networks' found in raw packet",
                        self._instance,
                        self._name,
                    )

                # Check how many times we got a network error, after 5 times it won't happen
                # anymore, thus we disable error reporting
                self._network_error += 1
                if self._network_error > 5:
                    _LOGGER.error(
                        "[%s] %s: Too many errors on 'networks' stats, disabling monitoring",
                        self._instance,
                        self._name,
                    )
                    self._info[CONTAINER_INFO_NETWORK_AVAILABLE] = False

        # All information collected
        stats["cpu"] = cpu_stats
        stats["memory"] = memory_stats
        stats["network"] = network_stats

        stats[CONTAINER_STATS_CPU_PERCENTAGE] = cpu_stats.get("total")
        if "online_cpus" in cpu_stats and cpu_stats.get("total") is not None:
            stats[CONTAINER_STATS_1CPU_PERCENTAGE] = round(
                cpu_stats.get("total") / cpu_stats["online_cpus"],
                self._config[CONF_PRECISION_CPU],
            )

        stats[CONTAINER_STATS_MEMORY] = memory_stats.get("usage")
        stats[CONTAINER_STATS_MEMORY_PERCENTAGE] = memory_stats.get("usage_percent")
        stats[CONTAINER_STATS_NETWORK_SPEED_UP] = network_stats.get("speed_tx")
        stats[CONTAINER_STATS_NETWORK_SPEED_DOWN] = network_stats.get("speed_rx")
        stats[CONTAINER_STATS_NETWORK_TOTAL_UP] = network_stats.get("total_tx")
        stats[CONTAINER_STATS_NETWORK_TOTAL_DOWN] = network_stats.get("total_rx")

        self._stats = stats

    #############################################################
    def cancel_task(self) -> None:
        if self._task is not None:
            _LOGGER.info(
                "[%s] %s: Cancelling task for container info/stats",
                self._instance,
                self._name,
            )
            self._task.cancel()
        else:
            _LOGGER.info(
                "[%s] %s: Task (not running) can not be cancelled for container info/stats",
                self._instance,
                self._name,
            )

    #############################################################
    def rename_entities_containername(self) -> None:
        if len(self._subscribers) > 0:
            _LOGGER.debug(
                "[%s] %s: Renaming entities for container", self._instance, self._name
            )

        for callback in self._subscribers:
            callback(rename=True, name=self._name)

    #############################################################
    def remove_entities(self) -> None:
        if len(self._subscribers) > 0:
            _LOGGER.debug(
                "[%s] %s: Removing entities from container", self._instance, self._name
            )

        for callback in self._subscribers:
            callback(remove=True)

        self._subscriber: list[Callable] = []

    #############################################################
    async def _start(self) -> None:
        """Separate loop to start container, because HA loop can't be used."""

        try:
            await self._container.start()
        except Exception as err:
            _LOGGER.error(
                "[%s] %s: Can not start container (%s)",
                self._instance,
                self._name,
                str(err),
            )
        finally:
            self._busy = False

    #############################################################
    async def start(self) -> None:
        """Called from HA switch."""
        _LOGGER.info("[%s] %s: Start container", self._instance, self._name)

        self._busy = True
        await self._start()

    #############################################################
    async def _stop(self) -> None:
        """Separate loop to stop container, because HA loop can't be used."""
        try:
            await self._container.stop(t=10)
        except Exception as err:
            _LOGGER.error(
                "[%s] %s: Can not stop container (%s)",
                self._instance,
                self._name,
                str(err),
            )
        finally:
            self._busy = False

    #############################################################
    async def stop(self) -> None:
        """Called from HA switch."""
        _LOGGER.info("[%s] %s: Stop container", self._instance, self._name)

        self._busy = True
        await self._stop()

    #############################################################
    async def _restart(self) -> None:
        """Separate loop to stop container, because HA loop can't be used."""
        try:
            await self._container.restart()
        except Exception as err:
            _LOGGER.error(
                "[%s] %s: Can not restart container (%s)",
                self._instance,
                self._name,
                str(err),
            )
        finally:
            self._busy = False

    #############################################################
    async def _restart_button(self) -> None:
        """Called from HA button."""
        _LOGGER.info("[%s] %s: Restart container", self._instance, self._name)

        self._busy = True
        await self._restart()

    #############################################################
    async def restart(self) -> None:
        """Called from service call."""
        _LOGGER.info("[%s] %s: Restart container", self._instance, self._name)

        self._busy = True
        await self._restart()

    #############################################################
    def get_name(self) -> str:
        """Return the container name."""
        return self._name

    #############################################################
    def set_name(self, name: str) -> None:
        """Set the container name."""
        self._name = name

    #############################################################
    def get_info(self) -> dict:
        """Return the container info."""
        return self._info

    #############################################################
    def get_stats(self) -> dict:
        """Return the container stats."""
        return self._stats

    #############################################################
    def get_api(self) -> DockerAPI:
        """Return the container stats."""
        return self._api

    #############################################################
    def register_callback(self, callback: Callable, variable: str):
        """Register callback from sensor/switch/button."""
        if callback not in self._subscribers:
            _LOGGER.debug(
                "[%s] %s: Added callback to container, entity: %s",
                self._instance,
                self._name,
                variable,
            )
            self._subscribers.append(callback)

    #############################################################
    def _notify(self) -> None:
        if len(self._subscribers) > 0:
            _LOGGER.debug(
                "[%s] %s: Send notify (%d) to container",
                self._instance,
                self._name,
                len(self._subscribers),
            )

        for callback in self._subscribers:
            callback()

    #############################################################
    @staticmethod
    def _calcdockerformat(dt: datetime) -> str:
        """Calculate datetime to Docker format, because it isn't available in stats."""
        if dt is None:
            return "None"

        delta = relativedelta.relativedelta(datetime.now(timezone.utc), dt)

        if delta.years != 0:
            return "{} {}".format(delta.years, "year" if delta.years == 1 else "years")
        elif delta.months != 0:
            return "{} {}".format(
                delta.months, "month" if delta.months == 1 else "months"
            )
        elif delta.days != 0:
            return "{} {}".format(delta.days, "day" if delta.days == 1 else "days")
        elif delta.hours != 0:
            return "{} {}".format(delta.hours, "hour" if delta.hours == 1 else "hours")
        elif delta.minutes != 0:
            return "{} {}".format(
                delta.minutes, "minute" if delta.minutes == 1 else "minutes"
            )

        return "{} {}".format(
            delta.seconds, "second" if delta.seconds == 1 else "seconds"
        )


#################################################################
class DockerContainerEntity(Entity):
    """Generic entity functions."""

    def __init__(
        self, container: DockerContainerAPI, instance: str, cname: str
    ) -> None:
        """Initialize the base for Container entities."""
        container_info = container.get_info()

        container_manufacturer = None
        container_image = None
        container_version = None

        image = container_info[CONTAINER_INFO_IMAGE]
        if image is not None and image != "":
            # Image can be of the form {Host}/{Publisher}/{Image}:{version},
            # where host, publisher and version can be optional
            image_parts = image.split("/")

            # If there are more than 2 parts, we can get the publisher
            if len(image_parts) > 1: 
                container_manufacturer = image_parts[-2].capitalize()
            
            # Split the last part again to retrieve possible version info
            parts = image_parts[-1].split(":")
            if len(parts) == 2:
                container_version = parts[1]
            container_image = parts[0]
        
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{instance}_container_{cname}")},
            name=cname.capitalize(),
            manufacturer=container_manufacturer,
            model=container_image,
            sw_version=container_version,
            entry_type=DeviceEntryType.SERVICE,
            via_device=(DOMAIN, f"{instance}_{container._config[CONF_URL]}"),
        )