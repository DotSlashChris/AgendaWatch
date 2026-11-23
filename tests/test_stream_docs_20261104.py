"""Nov 4 2026: the daily run was OOM-killed (exit -9) holding 100-185 MB PMN packets in memory.
Attachments now stream to disk in 1 MiB chunks; text, length and headers are unchanged."""
import agenda_watch as aw


class Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self._body, self.headers, self.closed = status, body, headers or {}, False
        self.chunks = 0

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            self.chunks += 1
            yield self._body[i:i + n]

    @property
    def content(self):
        raise AssertionError("whole body must not be loaded into memory")

    def close(self):
        self.closed = True


def test_streams_to_disk_never_reads_content(monkeypatch, tmp_path):
    body = b"%PDF-1.7\n" + b"x" * (3 * aw.DOC_CHUNK + 5)
    seen = {}

    def get(url, timeout=None, headers=None, stream=False):
        seen["stream"] = stream
        seen["r"] = Resp(200, body, {"ETag": "e1", "Last-Modified": "lm"})
        return seen["r"]
    monkeypatch.setattr(aw.S, "get", get)
    dest = tmp_path / "a.pdf"
    status, n, h = aw.get_doc_to_file("https://www.utah.gov/pmn/files/1.pdf", dest)
    assert (status, n, h["ETag"]) == (200, len(body), "e1")
    assert dest.read_bytes() == body and seen["stream"] is True and seen["r"].closed and seen["r"].chunks == 4


def test_fetch_document_same_row_and_304(monkeypatch):
    body = b"%PDF-1.4\nhello"
    monkeypatch.setattr(aw.S, "get", lambda url, timeout=None, headers=None, stream=False:
                        Resp(304) if headers else Resp(200, body, {"ETag": "e"}))
    monkeypatch.setattr(aw, "extract_pdf_text", lambda p: "TEXT " + str(p.stat().st_size))
    text, row, how = aw.fetch_document("https://www.utah.gov/pmn/files/9.pdf", "n", None)
    assert (text, how, row["length"], row["etag"]) == (f"TEXT {len(body)}", "fetched", len(body), "e")
    assert not list(aw.CACHE.glob("*"))  # temp PDF removed
    text2, row2, how2 = aw.fetch_document("https://www.utah.gov/pmn/files/9.pdf", "n", dict(row, text="OLD"))
    assert (text2, row2, how2) == ("OLD", None, "304")


def test_not_a_pdf_rejected_and_cleaned(monkeypatch):
    monkeypatch.setattr(aw.S, "get", lambda url, timeout=None, headers=None, stream=False: Resp(200, b"<html>"))
    try:
        aw.fetch_document("https://www.utah.gov/pmn/files/8.pdf", "n", None)
        raise AssertionError("expected FetchError")
    except aw.FetchError as ex:
        assert "not a PDF" in str(ex)
    assert not list(aw.CACHE.glob("*"))
