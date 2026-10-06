"""
OpenDrop: an open source AirDrop implementation
Copyright (C) 2026  Saikarthik Ramakrishnan

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
import struct
import zlib

# DVZip is the upload container of current iOS versions
# (Content-Type: application/x-dvzip). It is a sequence of blocks:
#
#     [4-byte big-endian header][payload]
#
# Bit 31 of the header marks a stored payload, otherwise the payload is a zlib
# stream. The low 31 bits are the payload length. Each block holds BLOCK_SIZE
# bytes of decoded data except the last. The decoded stream is a cpio archive.

BLOCK_SIZE = 128 * 1024
STORED = 0x80000000
# zlib adds a few bytes to incompressible data; anything far beyond that is bogus
MAX_PAYLOAD = BLOCK_SIZE + 1024


def blocks(data, block_size=BLOCK_SIZE):
    """
    Yield the encoded blocks of `data`, compressing only when it helps
    """
    for offset in range(0, len(data), block_size):
        chunk = data[offset : offset + block_size]
        packed = zlib.compress(chunk)
        if len(packed) < len(chunk):
            yield struct.pack(">I", len(packed)) + packed
        else:
            yield struct.pack(">I", STORED | len(chunk)) + chunk


def encode(data, block_size=BLOCK_SIZE):
    return b"".join(blocks(data, block_size))


class DvzipReader(io.RawIOBase):
    """
    Decode a DVZip stream on the fly. Senders are untrusted, so every block is
    checked against the format's size limits before it is used.
    """

    def __init__(self, raw):
        super().__init__()
        self.raw = raw
        self.block = b""
        self.done = False

    def readable(self):
        return True

    def _read_exact(self, length):
        data = bytearray()
        while len(data) < length:
            part = self.raw.read(length - len(data))
            if not part:
                break
            data += part
        return bytes(data)

    def _next_block(self):
        header = self._read_exact(4)
        if not header:
            self.done = True
            return
        if len(header) < 4:
            raise ValueError("Truncated DVZip block header")
        (value,) = struct.unpack(">I", header)
        length = value & ~STORED
        if length == 0:
            self.done = True
            return
        if length > MAX_PAYLOAD:
            raise ValueError(f"DVZip block too large ({length} bytes)")
        payload = self._read_exact(length)
        if len(payload) < length:
            raise ValueError("Truncated DVZip block")
        if value & STORED:
            block = payload
        else:
            decompressor = zlib.decompressobj()
            try:
                # Cap the output so a small block cannot expand without limit
                block = decompressor.decompress(payload, BLOCK_SIZE)
            except zlib.error as e:
                raise ValueError(f"Invalid DVZip block: {e}") from e
            if decompressor.unconsumed_tail or not decompressor.eof:
                raise ValueError("Invalid DVZip block")
        if len(block) > BLOCK_SIZE:
            raise ValueError("DVZip block decodes to more than 128 KiB")
        self.block = block

    def readinto(self, buf):
        while not self.block and not self.done:
            self._next_block()
        length = min(len(buf), len(self.block))
        buf[:length] = self.block[:length]
        self.block = self.block[length:]
        return length
