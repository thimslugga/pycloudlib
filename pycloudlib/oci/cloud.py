# pylint: disable=E1101
# This file is part of pycloudlib. See LICENSE file for license information.
"""OCI Cloud type."""

import base64
import json
import os
import re

import oci

from pycloudlib.cloud import BaseCloud
from pycloudlib.config import ConfigFile
from pycloudlib.oci.instance import OciInstance
from pycloudlib.oci.utils import get_subnet_id, wait_till_ready
from pycloudlib.util import UBUNTU_RELEASE_VERSION_MAP, subp


class OCI(BaseCloud):
    """OCI (Oracle) cloud class."""

    _type = "oci"

    def __init__(
        self,
        tag,
        timestamp_suffix=True,
        config_file: ConfigFile = None,
        *,
        availability_domain=None,
        compartment_id=None,
        config_path=None,
        config_dict=None,
    ):
        """
        Initialize the connection to OCI.

        OCI must be initialized on the CLI first:
        https://github.com/cloud-init/qa-scripts/blob/master/doc/launching-oracle.md

        Args:
            tag: Name of instance
            timestamp_suffix: bool set True to append a timestamp suffix to the
                tag
            config_file: path to pycloudlib configuration file
            compartment_id: A compartment found at
                https://console.us-phoenix-1.oraclecloud.com/a/identity/compartments
            availability_domain: One of the availability domains from:
                'oci iam availability-domain list'
            config_path: Path of OCI config file
            config_dict: A dictionary containing the OCI config.
                Overrides the values from config_path
        """
        super().__init__(
            tag,
            timestamp_suffix,
            config_file,
            required_values=[availability_domain, compartment_id],
        )

        self.availability_domain = (
            availability_domain or self.config["availability_domain"]
        )

        compartment_id = compartment_id or self.config.get("compartment_id")
        if not compartment_id:
            command = ["oci", "iam", "compartment", "get"]
            exception_text = (
                "Could not obtain OCI compartment id. Has the CLI client been "
                "setup?\nCommand attempted: '{}'".format(" ".join(command))
            )
            try:
                result = subp(command, rcs=())
            except FileNotFoundError as e:
                raise Exception(exception_text) from e
            if not result.ok:
                exception_text += "\nstdout: {}\nstderr: {}".format(
                    result.stdout, result.stderr
                )
                raise Exception(exception_text)
            compartment_id = json.loads(result.stdout)["data"]["id"]
        self.compartment_id = compartment_id

        if config_dict:
            try:
                oci.config.validate_config(config_dict)
                self.oci_config = config_dict
            except oci.exceptions.InvalidConfig as e:
                raise ValueError(
                    "Config dict is invalid. Pass a valid config dict. "
                    "{}".format(e)
                ) from e

        else:
            config_path = (
                config_path
                or self.config.get("config_path")
                or "~/.oci/config"
            )
            if not os.path.isfile(os.path.expanduser(config_path)):
                raise ValueError(
                    "{} is not a valid config file. Pass a valid config "
                    "file.".format(config_path)
                )
            self.oci_config = oci.config.from_file(config_path)

        self._log.debug("Logging into OCI")
        self.compute_client = oci.core.ComputeClient(self.oci_config)
        self.network_client = oci.core.VirtualNetworkClient(self.oci_config)

    def delete_image(self, image_id):
        """Delete an image.

        Args:
            image_id: string, id of the image to delete
        """
        self.compute_client.delete_image(image_id)

    def released_image(self, release, operating_system="Canonical Ubuntu"):
        """Get the released image.

        OCI just has periodic builds, so "released" and "daily" don't
        really make sense here. Just call the same code for both

        Args:
            release: string, Ubuntu release to look for
            operating_system: string, operating system to use
        Returns:
            string, id of latest image

        """
        return self.daily_image(release, operating_system)

    def daily_image(self, release, operating_system="Canonical Ubuntu"):
        """Get the daily image.

        OCI just has periodic builds, so "released" and "daily" don't
        really make sense here. Just call the same code for both.

        Should be equivalent to the cli call:
        oci compute image list \
          --operating-system="Canonical Ubuntu" \
          --operating-system-version="<xx.xx>" \
          --sort-by='TIMECREATED' \
          --sort-order='DESC'

        Args:
            release: string, Ubuntu release to look for
            operating_system: string, Operating system to use

        Returns:
            string, id of latest image

        """
        if operating_system == "Canonical Ubuntu":
            if not re.match(r"^\d{2}\.\d{2}$", release):  # 18.04, 20.04, etc
                try:
                    release = UBUNTU_RELEASE_VERSION_MAP[release]
                except KeyError as e:
                    raise ValueError("Invalid release") from e

        # OCI likes to keep a few of each image around, so sort by
        # timestamp descending and grab the first (most recent) one
        image_response = self.compute_client.list_images(
            self.compartment_id,
            operating_system=operating_system,
            operating_system_version=release,
            sort_by="TIMECREATED",
            sort_order="DESC",
        )
        matching_image = [
            i
            for i in image_response.data
            if "aarch64" not in i.display_name and "GPU" not in i.display_name
        ]
        image_id = matching_image[0].id
        return image_id

    def image_serial(self, image_id):
        """Find the image serial of the latest daily image for a particular release.

        Args:
            image_id: string, Ubuntu image id

        Returns:
            string, serial of latest image

        """
        raise NotImplementedError

    def get_instance(self, instance_id):
        """Get an instance by id.

        Args:
            instance_id:
        Returns:
            An instance object to use to manipulate the instance further.
        """
        try:
            self.compute_client.get_instance(instance_id)
        except oci.exceptions.ServiceError as e:
            raise Exception(
                "Unable to retrieve instance with id: {} . "
                "Is it a valid instance id?".format(instance_id)
            ) from e

        return OciInstance(
            key_pair=self.key_pair,
            instance_id=instance_id,
            compartment_id=self.compartment_id,
            oci_config=self.oci_config,
        )

    def launch(
        self,
        image_id,
        instance_type="VM.Standard2.1",
        user_data=None,
        wait=True,
        **kwargs,
    ):
        """Launch an instance.

        Args:
            image_id: string, image ID to use for the instance
            instance_type: string, type of instance to create.
                https://docs.cloud.oracle.com/en-us/iaas/Content/Compute/References/computeshapes.htm
            user_data: used by Cloud-Init to run custom scripts or
                provide custom Cloud-Init configuration
            wait: wait for instance to be live
            **kwargs: dictionary of other arguments to pass as
                LaunchInstanceDetails

        Returns:
            An instance object to use to manipulate the instance further.
        Raises: ValueError on invalid image_id
        """
        if not image_id:
            raise ValueError(
                f"{self._type} launch requires image_id param."
                f" Found: {image_id}"
            )
        subnet_id = get_subnet_id(
            self.network_client, self.compartment_id, self.availability_domain
        )
        metadata = {
            "ssh_authorized_keys": self.key_pair.public_key_content,
        }
        if user_data:
            metadata["user_data"] = base64.b64encode(
                user_data.encode("utf8")
            ).decode("ascii")

        instance_details = oci.core.models.LaunchInstanceDetails(
            display_name=self.tag,
            availability_domain=self.availability_domain,
            compartment_id=self.compartment_id,
            shape=instance_type,
            subnet_id=subnet_id,
            image_id=image_id,
            metadata=metadata,
            **kwargs,
        )

        instance_data = self.compute_client.launch_instance(
            instance_details
        ).data
        instance = self.get_instance(instance_data.id)
        if wait:
            wait_till_ready(
                func=self.compute_client.get_instance,
                current_data=instance_data,
                desired_state="RUNNING",
            )
            instance.wait()
        return instance

    def snapshot(self, instance, clean=True, name=None):
        """Snapshot an instance and generate an image from it.

        Args:
            instance: Instance to snapshot
            clean: run instance clean method before taking snapshot
            name: (Optional) Name of created image
        Returns:
            An image object
        """
        if clean:
            instance.clean()
        image_details = {
            "compartment_id": self.compartment_id,
            "instance_id": instance.instance_id,
        }
        if name:
            image_details["display_name"] = name
        image_data = self.compute_client.create_image(
            oci.core.models.CreateImageDetails(**image_details)
        ).data
        image_data = wait_till_ready(
            func=self.compute_client.get_image,
            current_data=image_data,
            desired_state="AVAILABLE",
        )

        return image_data.id
