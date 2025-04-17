"""Update functionality for the Monitor Docker integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.update import UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .helpers import DockerContainerAPI
from .const import (
    API,
    ATTR_NAME,
    ATTR_SERVER,
    CONF_CONTAINERS,
    CONF_CONTAINERS_EXCLUDE,
    CONF_NAME,
    CONF_BUTTONENABLED,
    CONFIG,
    CONTAINER,
    CONTAINER_INFO_STATE,
    DOMAIN,
    SERVICE_RESTART,
)



_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Update entity set up for Hass.io config entry."""
    await async_setup_platform(
        hass=hass,
        config=config_entry.data,
        async_add_entities=async_add_entities,
        discovery_info={"name": config_entry.data[CONF_NAME]},
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
):
    """Set up the Monitor Docker Button."""

    if discovery_info is None:
        return

    instance = discovery_info[CONF_NAME]
    name = discovery_info[CONF_NAME]
    api = hass.data[DOMAIN][name][API]
    config = hass.data[DOMAIN][name][CONFIG]

    _LOGGER.debug("[%s]: Setting up update entity", instance)

    buttons = []

    # We support add/re-add of a container
    if CONTAINER in discovery_info:
        clist = [discovery_info[CONTAINER]]
    else:
        clist = api.list_containers()

    for cname in clist:
        includeContainer = False
        if cname in config[CONF_CONTAINERS] or not config[CONF_CONTAINERS]:
            includeContainer = True

        if config[CONF_CONTAINERS_EXCLUDE] and cname in config[CONF_CONTAINERS_EXCLUDE]:
            includeContainer = False

        if includeContainer:
            if (
                config[CONF_BUTTONENABLED] == True
                or cname in config[CONF_BUTTONENABLED]
            ):
                _LOGGER.debug("[%s] %s: Adding component Button", instance, cname)

                buttons.append(
                    DockerContainerButton(
                        api.get_container(cname),
                        instance=instance,
                        cname=cname,
                    )
                )
            else:
                _LOGGER.debug("[%s] %s: NOT Adding component Button", instance, cname)

    if not buttons:
        _LOGGER.info("[%s]: No containers set-up", instance)
        return False

    async_add_entities(buttons, True)

    # platform = entity_platform.current_platform.get()
    # platform.async_register_entity_service(SERVICE_RESTART, {}, "async_restart")
    hass.services.async_register(
        DOMAIN, SERVICE_RESTART, async_restart, schema=SERVICE_RESTART_SCHEMA
    )

    return True


class DockerUpdateEntity(UpdateEntity):
    """Class to represent a Docker container update entity."""

    def __init__(self, hass: HomeAssistant, container_api: DockerContainerAPI, cname: str):
        """Initialize the Docker update entity."""
        self.hass = hass
        self.container_api = container_api
        self.cname = cname
        self._attr_name = f"Docker Update - {cname}"
        self._attr_unique_id = f"docker_update_{cname}"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_installed_version = None
        self._attr_latest_version = None
        self._attr_in_progress = False

    async def async_update(self):
        """Fetch the latest data for the Docker container."""
        try:
            status = await self.container_api.get_status()
            self._attr_installed_version = status.get("installed_version")
            self._attr_latest_version = status.get("latest_version")
        except Exception as err:
            _LOGGER.error("Error updating container %s: %s", self.cname, err)

    async def async_install(self, version: str = None, backup: bool = False):
        """Restart the Docker container to apply updates."""
        try:
            self._attr_in_progress = True
            await self.container_api.restart()
            _LOGGER.info("Successfully restarted container: %s", self.cname)
        except Exception as err:
            _LOGGER.error("Failed to restart container %s: %s", self.cname, err)
        finally:
            self._attr_in_progress = False

    @property
    def installed_version(self) -> str | None:
        """Version currently in use."""
        return self._attr_installed_version

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install."""
        return self._attr_latest_version
    
    @property
    def release_url(self) -> str | None:
        """URL to the release notes."""
        return None
    
    @property
    def in_pogress(self) -> str | None:
        """Update is in progress."""
        return self._attr_in_progress 