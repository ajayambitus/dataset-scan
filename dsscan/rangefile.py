"""A read-only, seekable file over S3 ranged GETs, so zipfile can list and read members of a huge archive
without downloading it (the central directory sits at the end; members are read on demand)."""
import io


class S3RangeFile(io.RawIOBase):
    def __init__(self, s3, bucket, key, size, block=8 * 1024 * 1024):
        self.s3, self.bucket, self.key, self.size, self.block = s3, bucket, key, size, block
        self.pos = 0
        self.cache = {}  # block index -> bytes (small LRU)
        self.fetched = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        self.pos = {io.SEEK_SET: offset, io.SEEK_CUR: self.pos + offset, io.SEEK_END: self.size + offset}[whence]
        return self.pos

    def _block(self, i):
        if i not in self.cache:
            start = i * self.block
            end = min(self.size, start + self.block) - 1
            data = self.s3.get_object(Bucket=self.bucket, Key=self.key, Range=f"bytes={start}-{end}")["Body"].read()
            self.fetched += len(data)
            if len(self.cache) > 8:
                self.cache.pop(next(iter(self.cache)))
            self.cache[i] = data
        return self.cache[i]

    def read(self, n=-1):
        if self.pos >= self.size:
            return b""
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        out = bytearray()
        while n > 0:
            i, off = divmod(self.pos, self.block)
            chunk = self._block(i)[off:off + n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)
