from homeassistant.components.update import UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
import logging
from custom_components.monitor_docker.helpers import DockerContainerAPI

"""Update functionality for the Monitor Docker integration."""

_LOGGER = logging.getLogger(__name__)

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