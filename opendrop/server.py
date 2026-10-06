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
import json
import logging
import os
import platform
import plistlib
import shutil
import socket
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import libarchive
import libarchive.extract
import libarchive.read
from zeroconf import IPVersion, ServiceInfo, Zeroconf

from .dvzip import DvzipReader
from .ui import clean
from .util import AirDropUtil

logger = logging.getLogger(__name__)

# Senders are untrusted: refuse "../" and absolute paths and writes through symlinks
SECURE_EXTRACT = (
    libarchive.extract.EXTRACT_SECURE_NODOTDOT
    | libarchive.extract.EXTRACT_SECURE_NOABSOLUTEPATHS
    | libarchive.extract.EXTRACT_SECURE_SYMLINKS
)


class CheckedStream(io.RawIOBase):
    """
    libarchive turns an exception raised while reading into a quiet end of
    file, which a short archive could survive. Remember the error instead, so
    the transfer can be refused after extraction.
    """

    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        self.error = None

    def readable(self):
        return True

    def readinto(self, buf):
        if self.error is not None:
            return 0
        try:
            return self.stream.readinto(buf)
        except (ValueError, OSError) as e:
            self.error = e
            return 0


def extract_archive(stream):
    """
    Extract an untrusted archive into the current directory. Files are staged
    first and only moved into place once the whole archive was read, without
    replacing existing files. Returns the names they were saved under.
    """
    directory = os.getcwd()
    staging = tempfile.mkdtemp(prefix=".opendrop-", dir=directory)
    try:
        checked = CheckedStream(stream)
        with libarchive.read.stream_reader(checked) as archive:

            def staged_entries():
                for entry in archive:
                    # AirDrop only sends files and folders
                    if not (entry.isfile or entry.isdir):
                        raise ValueError(f"Unsupported entry type: {entry.pathname}")
                    # relative, since extraction refuses absolute paths
                    entry.pathname = os.path.join(
                        os.path.relpath(staging), entry.pathname
                    )
                    yield entry

            libarchive.extract.extract_entries(staged_entries(), SECURE_EXTRACT)
        if checked.error is not None:
            raise ValueError(f"Upload failed: {checked.error}")
        return [
            _move_without_replacing(os.path.join(staging, name), directory)
            for name in sorted(os.listdir(staging))
        ]
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _move_without_replacing(path, directory):
    base, ext = os.path.splitext(os.path.basename(path))
    name, number = base + ext, 2
    while os.path.lexists(os.path.join(directory, name)):
        name, number = f"{base} {number}{ext}", number + 1
    os.rename(path, os.path.join(directory, name))
    return name


class AirDropServer:
    """
    Announces an HTTPS AirDrop server in the local network via mDNS.
    """

    def __init__(self, config):
        self.config = config

        # Use IPv6
        self.serveraddress = ("::", self.config.port)
        self.ServerClass = HTTPServerV6
        self.ServerClass.allow_reuse_address = False

        self.ip_addr = AirDropUtil.get_ip_for_interface(
            self.config.interface, ipv6=True
        )
        if self.ip_addr is None:
            if self.config.interface == "awdl0":
                raise RuntimeError(
                    f"Interface {self.config.interface} does not have an IPv6 address. Make sure that `owl` is running."
                )
            else:
                raise RuntimeError(
                    f"Interface {self.config.interface} does not have an IPv6 address"
                )

        self.Handler = AirDropServerHandler
        self.Handler.config = self.config

        self.zeroconf = Zeroconf(
            interfaces=[str(self.ip_addr)],
            ip_version=IPVersion.V6Only,
            apple_p2p=platform.system() == "Darwin",
        )

        self.http_server = self._init_server()
        self.service_info = self._init_service()

    def _init_service(self):
        properties = self.get_properties()
        server = self.config.host_name + ".local."
        service_name = self.config.service_id + "._airdrop._tcp.local."
        info = ServiceInfo(
            "_airdrop._tcp.local.",
            service_name,
            port=self.config.port,
            properties=properties,
            server=server,
            addresses=[self.ip_addr.packed],
        )
        return info

    def start_service(self):
        logger.info(
            f"Announcing service: host {self.config.host_name}, address {self.ip_addr}, port {self.config.port}"
        )
        self.zeroconf.register_service(self.service_info)

    def _init_server(self):
        try:
            httpd = self.ServerClass(self.serveraddress, self.Handler)
        except OSError:
            # Address in use. Change port
            self.config.port = self.config.port + 1
            self.serveraddress = (self.serveraddress[0], self.config.port)
            httpd = self.ServerClass(self.serveraddress, self.Handler)

        # Adapt socket for awdl0
        if self.config.interface == "awdl0" and platform.system() == "Darwin":
            httpd.socket.setsockopt(socket.SOL_SOCKET, 0x1104, 1)

        httpd.socket = self.config.get_ssl_context().wrap_socket(
            sock=httpd.socket, server_side=True
        )

        return httpd

    def start_server(self):
        logger.info("Starting HTTPS server")
        self.http_server.serve_forever()

    def stop(self):
        self.zeroconf.unregister_all_services()
        self.http_server.shutdown()

    def get_properties(self):
        properties = {b"flags": str(self.config.flags).encode("utf-8")}
        return properties


class HTTPServerV6(ThreadingMixIn, HTTPServer):
    # Threads, so a sender waiting for the user to accept doesn't block others
    address_family = socket.AF_INET6
    daemon_threads = True


class AirDropServerHandler(BaseHTTPRequestHandler):
    """
    Server which responds to AirDrop HTTP POST requests
    """

    protocol_version = "HTTP/1.1"
    config = None
    # Transfers the user accepted, by TransferID, until their upload arrives
    accepted = {}
    # Set on this connection's handler once its /Ask was accepted
    approved = None

    def _set_response(self, content_length):
        """
        Setting the default values for a successful response
        """
        self.send_response(200)
        self.send_header("Content-Length", content_length)
        self.end_headers()

    def _send_empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", 0)
        self.send_header("Connection", "close")
        self.end_headers()

    def _read_body(self):
        """
        Read the request body, sent either with a Content-Length or chunked
        """
        if self.headers.get("transfer-encoding", "").lower() != "chunked":
            return self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = bytearray()
        while True:
            length = int(self.rfile.readline().split(b";")[0].strip(), 16)
            if length == 0:
                # skip optional trailers up to the terminating empty line
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return bytes(body)
            body += self.rfile.read(length)
            self.rfile.readline()  # CRLF after each chunk

    def do_HEAD(self):
        """
        Answer head requests
        """
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

    def do_GET(self):
        """
        Answer get requests
        """
        logger.debug(f"GET request at {self.path}")
        body = "\n".encode("utf-8")
        self._set_response(len(body))
        self.wfile.write(body)

    def handle_discover(self):
        post_data = self._read_body()

        AirDropUtil.write_debug(
            self.config, post_data, "receive_discover_request.plist"
        )

        # sample media capabilities as recorded from macOS 10.13.3
        media_capabilities = {
            "Version": 1,
            # don't advertise any codec/container support so we receive legacy file formats (JPEG instead of HEIF, etc.)
            # 'Codecs': {
            #     'hvc1': {
            #         'Profiles': {
            #             'VTPerProfileSupport': {
            #                 '1': {'VTMaxPlaybackLevel': 120},
            #                 '2': {'VTMaxPlaybackLevel': 120},
            #                 '3': {}
            #             },
            #             'VTSupportedProfiles': [1, 2, 3]
            #         }
            #     }
            # },
            # 'ContainerFormats': {
            #     'public.heif-standard': {
            #         'HeifSubtypes': ['public.avci', 'public.heic', 'public.heif']
            #     }
            # },
            # 'Vendor': {
            #     'com.apple': {
            #         'OSVersion': [10, 13, 3],
            #         'OSBuildVersion': '17D102',
            #         'LivePhotoFormatVersion': '1'
            #     }
            # }
        }
        media_capabilities_json = json.JSONEncoder().encode(media_capabilities)
        media_capabilities_binary = media_capabilities_json.encode("utf-8")
        discover_answer = {
            "ReceiverMediaCapabilities": media_capabilities_binary,
            "ReceiverComputerName": self.config.computer_name,
            "ReceiverModelName": self.config.computer_model,
        }
        if self.config.record_data:
            discover_answer["ReceiverRecordData"] = self.config.record_data

        discover_answer_binary = plistlib.dumps(
            discover_answer, fmt=plistlib.FMT_BINARY  # pylint: disable=no-member
        )

        AirDropUtil.write_debug(
            self.config, discover_answer_binary, "receive_discover_response.plist"
        )

        # Change to actual length
        self._set_response(len(discover_answer_binary))
        self.wfile.write(discover_answer_binary)

    def handle_ask(self):
        post_data = self._read_body()

        AirDropUtil.write_debug(self.config, post_data, "receive_ask_request.plist")

        try:
            ask = plistlib.loads(post_data)
        except Exception:  # pylint: disable=broad-except
            ask = None
        if not isinstance(ask, dict):
            logger.warning("Rejected malformed /Ask request")
            self._send_empty(400)
            return

        if self.config.confirm is not None and not self.config.confirm(ask):
            logger.info("Transfer declined")
            self._send_empty(403)  # the sender shows "Declined"
            return

        self.approved = ask
        transfer_id = ask.get("TransferID")
        if isinstance(transfer_id, dict) and transfer_id.get("id"):
            self.accepted[str(transfer_id["id"])] = ask

        ask_response = {
            "ReceiverModelName": self.config.computer_model,
            "ReceiverComputerName": self.config.computer_name,
        }
        ask_resp_binary = plistlib.dumps(
            ask_response, fmt=plistlib.FMT_BINARY  # pylint: disable=no-member
        )

        AirDropUtil.write_debug(
            self.config, ask_resp_binary, "receive_ask_response.plist"
        )

        self._set_response(len(ask_resp_binary))
        self.wfile.write(ask_resp_binary)

    def handle_upload(self):
        if self.headers.get("content-type", "").lower() not in (
            "application/x-cpio",
            "application/x-dvzip",
        ):
            logger.warning(
                f"Unsupported content-type: {self.headers.get('content-type')}"
            )
            # Unlike _send_empty, also tells the sender what we do accept
            self.send_response(406)  # Unprocessable Entity
            self.send_header("Content-Type", "application/x-cpio")
            self.send_header("Content-Length", 0)
            self.send_header("Connection", "close")
            self.end_headers()
            return

        # Only accept uploads for a transfer the user agreed to, matched by the
        # TransferID that current iOS repeats from /Ask or by this connection
        ask = self.accepted.pop(self.headers.get("TransferID", ""), None)
        ask = ask or self.approved
        if ask is None:
            logger.warning("Rejected upload without an accepted /Ask")
            self._send_empty(403)
            return
        self.approved = None

        # If pipelining is not support, 'Expect: 100-continue' is sent to which we need to respond
        if self.headers.get("expect", "").lower() == "100-continue":
            self.send_response(100)
            self.send_header("Content-Length", 0)
            self.end_headers()

        if self.headers.get("transfer-encoding", "").lower() != "chunked":
            logger.warning("Expect chunked transfer encoding")
            self.send_response(400)  # Bad Request
            self.send_header("Transfer-Encoding", "Chunked")
            self.send_header("Content-Length", 0)
            self.send_header("Connection", "close")
            self.end_headers()
            return

        class HTTPChunkedReader(io.RawIOBase):
            def __init__(self, rfile, keep=False):
                super().__init__()
                self.rfile = rfile
                self.chunk = None
                self.total = 0
                self.raw = bytearray() if keep else None  # for --debug captures

            def _next_chunk(self):
                if self.chunk is None or len(self.chunk) == 0:
                    length = int(self.rfile.readline().rstrip(), 16)
                    self.chunk = self.rfile.read(length)
                    self.rfile.readline()  # strip trailing \n\r

            def readinto(self, buf):
                self._next_chunk()
                length = min(len(self.chunk), len(buf))
                buf[:length] = self.chunk[:length]
                self.chunk = self.chunk[length:]
                self.total += length
                if self.raw is not None:
                    self.raw += buf[:length]
                return length

        logger.info("Receiving file(s) ...")
        start = time.time()
        reader = HTTPChunkedReader(self.rfile, keep=self.config.debug)
        stream = reader
        if self.headers.get("content-type", "").lower() == "application/x-dvzip":
            stream = DvzipReader(reader)
        try:
            saved = extract_archive(stream)
        except (libarchive.ArchiveError, ValueError) as e:
            logger.warning(f"Rejected unsafe or malformed archive: {e}")
            self._send_empty(400)
            return
        finally:
            if reader.raw is not None:
                AirDropUtil.write_debug(
                    self.config, bytes(reader.raw), "receive_upload_request.bin"
                )

        transferred = reader.total / 1024.0 / 1024.0
        speed = transferred / (time.time() - start)
        logger.info(
            f"Saved {', '.join(saved)} "
            f"(size {transferred:.02f} MB, speed {speed:.02f} MB/s)"
        )
        if self.config.notify is not None:
            sender = clean(ask.get("SenderComputerName") or "someone nearby")
            what = clean(saved[0]) if len(saved) == 1 else f"{len(saved)} items"
            self.config.notify(f"Received {what} from {sender}")

        self._send_empty(200)

    def do_POST(self):
        """
        Handle post requests
        """

        logger.debug(f"POST request at {self.path}")
        logger.debug(f"Headers\n{self.headers}")

        if self.path == "/Discover":
            self.handle_discover()
        elif self.path == "/Ask":
            self.handle_ask()
        elif self.path == "/Upload":
            self.handle_upload()
        else:
            logger.debug(f"POST request at {self.path}")
            self.send_response(400)
            self.send_header("Content-Length", 0)
            self.end_headers()

    def log_message(self, format, *args):
        # pylint: disable=redefined-builtin
        logger.debug(
            f"{self.client_address[0]} - - [{self.log_date_time_string()}] {format % args}"
        )
