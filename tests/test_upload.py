import io
import plistlib
import ssl
import threading
from http.client import HTTPSConnection

import libarchive
import pytest
from test_server import get_loopback

from opendrop import dvzip
from opendrop.client import AirDropClient
from opendrop.config import AirDropConfig
from opendrop.server import AirDropServer


def make_archive(pathname, data, filter_name="gzip"):
    """Build a cpio archive, the format AirDrop senders upload."""
    buf = io.BytesIO()
    with libarchive.custom_writer(buf.write, "cpio", filter_name=filter_name) as a:
        a.add_file_from_memory(pathname, len(data), data)
    return buf.getvalue()


class Receiver:
    def __init__(self, tmp_path):
        self.inbox = tmp_path / "inbox"
        self.inbox.mkdir()
        self.config = AirDropConfig(
            interface=get_loopback(),
            airdrop_dir=str(tmp_path / "config"),
            server_port=0,  # let the OS pick a free port
        )
        self.asks = []
        self.decision = True
        self.notes = []
        self.config.confirm = self._confirm
        self.config.notify = self.notes.append
        self.server = AirDropServer(self.config)
        self.port = self.server.http_server.server_address[1]

    def _confirm(self, ask):
        self.asks.append(ask)
        return self.decision


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    receiver = Receiver(tmp_path)
    monkeypatch.chdir(receiver.inbox)  # the server extracts into the current directory
    threading.Thread(target=receiver.server.start_server, daemon=True).start()
    yield receiver
    receiver.server.http_server.shutdown()
    receiver.server.zeroconf.close()


def connect(receiver):
    return HTTPSConnection(
        "::1", receiver.port, context=ssl._create_unverified_context()
    )


def ask(conn, transfer_id="T-1", name="hello.txt", size=2):
    body = {
        "SenderComputerName": "Test Phone",
        "TransferID": {"id": transfer_id},
        "Files": [{"FileName": name, "FileSize": size}],
    }
    conn.request("POST", "/Ask", body=plistlib.dumps(body, fmt=plistlib.FMT_BINARY))
    response = conn.getresponse()
    response.read()
    return response.status


def upload(conn, body, content_type="application/x-cpio", transfer_id=None):
    headers = {"Content-Type": content_type}
    if transfer_id:
        headers["TransferID"] = transfer_id
    conn.request(
        "POST", "/Upload", body=iter([body]), headers=headers, encode_chunked=True
    )
    return conn.getresponse().status


def test_accepted_transfer_is_extracted(receiver):
    conn = connect(receiver)
    assert ask(conn) == 200
    assert upload(conn, make_archive("./hello.txt", b"hi")) == 200
    assert (receiver.inbox / "hello.txt").read_bytes() == b"hi"
    assert receiver.asks[0]["SenderComputerName"] == "Test Phone"
    assert receiver.notes == ["Received hello.txt from Test Phone"]


def test_dvzip_upload_is_extracted(receiver):
    data = bytes(range(256)) * 2000  # spans several 128 KiB blocks
    conn = connect(receiver)
    assert ask(conn, name="photo.jpg", size=len(data)) == 200
    body = dvzip.encode(make_archive("./photo.jpg", data, filter_name=None))
    assert upload(conn, body, "application/x-dvzip") == 200
    assert (receiver.inbox / "photo.jpg").read_bytes() == data


def test_upload_matched_by_transfer_id_on_new_connection(receiver):
    assert ask(connect(receiver), transfer_id="T-42") == 200
    body = make_archive("./hello.txt", b"hi")
    assert upload(connect(receiver), body, transfer_id="T-42") == 200
    # an accepted transfer can only be uploaded once
    assert upload(connect(receiver), body, transfer_id="T-42") == 403


def test_declined_transfer_is_refused(receiver):
    receiver.decision = False
    conn = connect(receiver)
    assert ask(conn) == 403
    assert upload(connect(receiver), make_archive("./hello.txt", b"hi")) == 403
    assert not (receiver.inbox / "hello.txt").exists()


def test_upload_without_ask_is_refused(receiver):
    body = make_archive("./hello.txt", b"hi")
    assert upload(connect(receiver), body, transfer_id="never-asked") == 403
    assert not (receiver.inbox / "hello.txt").exists()


def test_malformed_ask_is_refused(receiver):
    conn = connect(receiver)
    conn.request("POST", "/Ask", body=b"not a plist")
    assert conn.getresponse().status == 400
    assert receiver.asks == []


@pytest.mark.parametrize("pathname", ["../escaped.txt", "./a/../../escaped.txt"])
def test_upload_rejects_parent_paths(receiver, pathname):
    conn = connect(receiver)
    assert ask(conn) == 200
    assert upload(conn, make_archive(pathname, b"x")) == 400
    assert not (receiver.inbox.parent / "escaped.txt").exists()


def test_upload_rejects_absolute_paths(receiver):
    target = receiver.inbox.parent / "absolute.txt"
    conn = connect(receiver)
    assert ask(conn) == 200
    assert upload(conn, make_archive(str(target), b"x")) == 400
    assert not target.exists()


def test_corrupt_dvzip_is_refused(receiver):
    conn = connect(receiver)
    assert ask(conn) == 200
    assert upload(conn, b"\x00\x00\x00\x05nope!", "application/x-dvzip") == 400


def test_client_sends_like_current_ios(receiver, tmp_path):
    photo = tmp_path / "photo.bin"
    photo.write_bytes(bytes(range(256)) * 1000)
    config = AirDropConfig(
        interface=get_loopback(), airdrop_dir=str(tmp_path / "sender")
    )
    client = AirDropClient(config, ("::1", receiver.port))
    assert client.send_ask(str(photo))
    assert client.send_upload(str(photo))
    assert (receiver.inbox / "photo.bin").read_bytes() == photo.read_bytes()

    ask_request = receiver.asks[0]
    assert ask_request["TransferID"] == {"id": client.transfer_id}
    assert ask_request["Files"][0]["FileSize"] == photo.stat().st_size


def test_existing_files_are_not_replaced(receiver):
    (receiver.inbox / "hello.txt").write_bytes(b"mine")
    conn = connect(receiver)
    assert ask(conn) == 200
    assert upload(conn, make_archive("./hello.txt", b"hi")) == 200
    assert (receiver.inbox / "hello.txt").read_bytes() == b"mine"
    assert (receiver.inbox / "hello 2.txt").read_bytes() == b"hi"


def test_links_are_refused(receiver):
    buf = io.BytesIO()
    with libarchive.custom_writer(buf.write, "cpio") as archive:
        archive.add_file_from_memory(
            "./link",
            0,
            b"",
            filetype=libarchive.entry.FileType.AE_IFLNK,
            linkpath="/etc/passwd",
        )
    conn = connect(receiver)
    assert ask(conn) == 200
    assert upload(conn, buf.getvalue()) == 400
    assert list(receiver.inbox.iterdir()) == []


def test_failed_upload_leaves_nothing_behind(receiver):
    data = bytes(range(256)) * 2000  # several blocks
    blocks = list(dvzip.blocks(make_archive("./big.bin", data, filter_name=None)))
    blocks[1] = blocks[1][:4] + b"\xff" * (len(blocks[1]) - 4)  # corrupt block 2
    conn = connect(receiver)
    assert ask(conn) == 200
    assert upload(conn, b"".join(blocks), "application/x-dvzip") == 400
    assert list(receiver.inbox.iterdir()) == []  # no partial file, no staging folder
