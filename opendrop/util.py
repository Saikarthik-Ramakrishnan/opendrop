"""
OpenDrop: an open source AirDrop implementation
Copyright (C) 2018  Milan Stute
Copyright (C) 2018  Alexander Heinrich

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import io
import ipaddress
import os

import ifaddr
from PIL import ExifTags, Image


class AirDropUtil:
    """
    This class contains a set of utility functions that support the opendrop implementation
    They have been moved, because the opendrop files tend to get too long
    """

    @staticmethod
    def get_uti_type(flp) -> str:
        """
        Get the Apple conform UTI Type from a flp instance which has been used on the data which should be sent

        :param flp: fleep object
        """

        # Default UTI Type
        uti_type = "public.content"
        if len(flp.mime) == 0 or len(flp.type) == 0:
            return uti_type

        mime = flp.mime[0]
        f_type = flp.type[0]
        if "image" in mime:
            uti_type = "public.image"

            if "jpg" in mime:
                uti_type = "public.jpeg"
            elif "jp2" in mime:
                uti_type = "public.jpeg-2000"
            elif "gif" in mime:
                uti_type = "com.compuserve.gif"
            elif "png" in mime:
                uti_type = "public.png"
            elif "raw" in mime or "raw" in f_type:
                uti_type = "public.camera-raw-image"
        elif "audio" in f_type:
            uti_type = "public.audio"
        elif "video" in f_type:
            uti_type = "public.video"
        elif "archive" in f_type:
            uti_type = "public.data"

            if "gzip" in mime:
                uti_type = "org.gnu.gnu-zip-archive"
            if "zip" in mime:
                uti_type = "public.zip-archive"

        return uti_type

    @staticmethod
    def generate_file_icon(file_path):
        """
        Generates a small and a big thumbnail of an image
        This will make it possible to preview the sent file

        :param file_path: The path to the image
        """
        im = Image.open(file_path)

        # rotate according to EXIF tags
        try:
            exif = dict(
                (ExifTags.TAGS[k], v)
                for k, v in im._getexif().items()  # pylint: disable=protected-access
                if k in ExifTags.TAGS
            )
            angles = {3: 180, 6: 270, 8: 90}
            orientation = exif["Orientation"]
            if orientation in angles.keys():
                im = im.rotate(angles[orientation], expand=True)
        except (AttributeError, KeyError):
            pass  # no EXIF data available

        # Big image
        im.thumbnail((540, 540), Image.Resampling.LANCZOS)
        img_bytes = io.BytesIO()
        im.save(img_bytes, format="JPEG2000")
        file_icon = img_bytes.getvalue()

        # Small image
        # im.thumbnail((64, 64), Image.Resampling.LANCZOS)
        # img_bytes = io.BytesIO()
        # im.save(img_bytes, format='JPEG2000')
        # small_file_icon = img_bytes.getvalue()

        return file_icon

    @staticmethod
    def get_ip_for_interface(interface_name, ipv6=False):
        """
        Get the ip address in IPv4 or IPv6 for a specific network interface

        :param str interface_name: declares the network interface name for which the ip should be accessed
        :param bool ipv6: Boolean indicating if the ipv6 address should be retrieved
        :return: IPv4Address or IPv6Address object or None
        """

        def get_interface_by_name(name):
            for interface in ifaddr.get_adapters():
                if interface.name == name:
                    return interface
            return None

        interface = get_interface_by_name(interface_name)
        if interface is None:
            return None

        for ip in interface.ips:
            if ip.is_IPv6 and ipv6:
                return ipaddress.IPv6Address(
                    ip.ip[0]
                )  # first of (ip, flowinfo, scope_id) tuple
            if ip.is_IPv4 and not ipv6:
                return ipaddress.IPv4Address(ip.ip)

        return None

    @staticmethod
    def write_debug(config, data, file_name):
        if not config.debug:
            return
        if not os.path.exists(config.debug_dir):
            os.makedirs(config.debug_dir)
        debug_file_path = os.path.join(config.debug_dir, file_name)
        with open(debug_file_path, "wb") as file:
            if hasattr(data, "read"):
                file.write(data.read())
                data.seek(0)  # reset cursor position
            else:  # assume bytes-like
                file.write(data)
