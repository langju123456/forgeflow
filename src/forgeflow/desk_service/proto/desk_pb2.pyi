from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal import containers as _containers

DESCRIPTOR: _descriptor.FileDescriptor

class SearchRequest(_message.Message):
    __slots__ = ("question", "gate", "top_k")
    QUESTION_FIELD_NUMBER: _ClassVar[int]
    GATE_FIELD_NUMBER: _ClassVar[int]
    TOP_K_FIELD_NUMBER: _ClassVar[int]
    question: str
    gate: bool
    top_k: int
    def __init__(self, question: str | None = ..., gate: bool | None = ..., top_k: int | None = ...) -> None: ...

class Citation(_message.Message):
    __slots__ = ("id", "doc", "company", "kind", "page", "bbox", "snippet")
    ID_FIELD_NUMBER: _ClassVar[int]
    DOC_FIELD_NUMBER: _ClassVar[int]
    COMPANY_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    BBOX_FIELD_NUMBER: _ClassVar[int]
    SNIPPET_FIELD_NUMBER: _ClassVar[int]
    id: str
    doc: str
    company: str
    kind: str
    page: int
    bbox: _containers.RepeatedScalarFieldContainer[float]
    snippet: str
    def __init__(self, id: str | None = ..., doc: str | None = ..., company: str | None = ..., kind: str | None = ..., page: int | None = ..., bbox: _Iterable[float] | None = ..., snippet: str | None = ...) -> None: ...

class Hit(_message.Message):
    __slots__ = ("id", "version", "content", "source_hash", "classification", "status", "citation")
    ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_HASH_FIELD_NUMBER: _ClassVar[int]
    CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CITATION_FIELD_NUMBER: _ClassVar[int]
    id: str
    version: str
    content: str
    source_hash: str
    classification: int
    status: str
    citation: Citation
    def __init__(self, id: str | None = ..., version: str | None = ..., content: str | None = ..., source_hash: str | None = ..., classification: int | None = ..., status: str | None = ..., citation: Citation | _Mapping | None = ...) -> None: ...

class SearchReply(_message.Message):
    __slots__ = ("kept", "dropped", "gate_status", "mode", "index_version", "retrieval_ms", "gate_ms")
    KEPT_FIELD_NUMBER: _ClassVar[int]
    DROPPED_FIELD_NUMBER: _ClassVar[int]
    GATE_STATUS_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    INDEX_VERSION_FIELD_NUMBER: _ClassVar[int]
    RETRIEVAL_MS_FIELD_NUMBER: _ClassVar[int]
    GATE_MS_FIELD_NUMBER: _ClassVar[int]
    kept: _containers.RepeatedCompositeFieldContainer[Hit]
    dropped: _containers.RepeatedCompositeFieldContainer[Citation]
    gate_status: str
    mode: str
    index_version: str
    retrieval_ms: float
    gate_ms: float
    def __init__(self, kept: _Iterable[Hit | _Mapping] | None = ..., dropped: _Iterable[Citation | _Mapping] | None = ..., gate_status: str | None = ..., mode: str | None = ..., index_version: str | None = ..., retrieval_ms: float | None = ..., gate_ms: float | None = ...) -> None: ...

class InfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class InfoReply(_message.Message):
    __slots__ = ("state", "detail", "mode", "index_version", "gate_available", "rows", "reports_json")
    STATE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    INDEX_VERSION_FIELD_NUMBER: _ClassVar[int]
    GATE_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    REPORTS_JSON_FIELD_NUMBER: _ClassVar[int]
    state: str
    detail: str
    mode: str
    index_version: str
    gate_available: bool
    rows: int
    reports_json: str
    def __init__(self, state: str | None = ..., detail: str | None = ..., mode: str | None = ..., index_version: str | None = ..., gate_available: bool | None = ..., rows: int | None = ..., reports_json: str | None = ...) -> None: ...

class PageImageRequest(_message.Message):
    __slots__ = ("doc", "page", "bbox")
    DOC_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    BBOX_FIELD_NUMBER: _ClassVar[int]
    doc: str
    page: int
    bbox: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, doc: str | None = ..., page: int | None = ..., bbox: _Iterable[float] | None = ...) -> None: ...

class PageImageReply(_message.Message):
    __slots__ = ("png",)
    PNG_FIELD_NUMBER: _ClassVar[int]
    png: bytes
    def __init__(self, png: bytes | None = ...) -> None: ...
