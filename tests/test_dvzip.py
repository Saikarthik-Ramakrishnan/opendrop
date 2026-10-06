import io
import os
import struct
import zlib

import pytest

from opendrop import dvzip


def decode(data):
    return dvzip.DvzipReader(io.BytesIO(data)).read()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hello",
        b"a" * (3 * dvzip.BLOCK_SIZE + 5),  # compressible, several blocks
        os.urandom(2 * dvzip.BLOCK_SIZE),  # incompressible, stored blocks
    ],
)
def test_round_trip(data):
    assert decode(dvzip.encode(data)) == data


def test_blocks_hold_128_kib_and_store_incompressible_data():
    blocks = list(dvzip.blocks(os.urandom(dvzip.BLOCK_SIZE + 1)))
    assert len(blocks) == 2
    (header,) = struct.unpack(">I", blocks[0][:4])
    assert header == dvzip.STORED | dvzip.BLOCK_SIZE


def test_zero_length_block_ends_stream():
    data = dvzip.encode(b"before") + struct.pack(">I", 0) + b"ignored"
    assert decode(data) == b"before"


@pytest.mark.parametrize(
    "data",
    [
        b"\x00\x00",  # truncated header
        struct.pack(">I", 10) + b"short",  # truncated payload
        struct.pack(">I", dvzip.MAX_PAYLOAD + 1),  # absurd length
        struct.pack(">I", 5) + b"nope!",  # not zlib
    ],
)
def test_rejects_malformed_streams(data):
    with pytest.raises(ValueError):
        decode(data)


def test_rejects_blocks_that_expand_beyond_128_kib():
    bomb = zlib.compress(b"\x00" * (10 * dvzip.BLOCK_SIZE))
    with pytest.raises(ValueError):
        decode(struct.pack(">I", len(bomb)) + bomb)
