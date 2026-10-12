"""Fail-closed collection completeness and explicitly contracted pagination."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .errors import (
    ApiResponseError,
    ConfigurationError,
    SnapshotConsistencyError,
    UnverifiedPaginationContract,
)

CompletenessAuthority = Literal[
    "OFFICIAL_FRONTEND_SINGLE_RESPONSE_CLOSURE",
    "EXPLICIT_PAGED_TOTAL",
    "EXPLICIT_SINGLE_RESPONSE_TOTAL",
]
PaginationMode = Literal["SINGLE_RESPONSE", "CURSOR", "PAGE_NUMBER"]
ContractPaginationMode = Literal["CURSOR", "PAGE_NUMBER"]

_PAGINATION_SIGNAL_FIELDS = frozenset(
    {
        "pagination",
        "paging",
        "pager",
        "pageinfo",
        "total",
        "totalcount",
        "hasmore",
        "hasnext",
        "cursor",
        "nextcursor",
        "next",
        "page",
        "pagenum",
        "pagesize",
        "limit",
        "offset",
        "nextpage",
        "islast",
    }
)
_PAGINATION_CONTAINER_FIELDS = frozenset({"pagination", "paging", "pager", "pageinfo"})


@dataclass(frozen=True, slots=True)
class PaginationContract:
    mode: ContractPaginationMode
    response_container_path: tuple[str, ...]
    total_field: str
    has_more_field: str
    next_cursor_response_field: str | None
    cursor_request_parameter: str | None
    current_page_response_field: str | None
    page_request_parameter: str | None
    start_page: int | None
    max_pages: int

    def __post_init__(self) -> None:
        field_names = (
            *self.response_container_path,
            self.total_field,
            self.has_more_field,
        )
        if any(not isinstance(field, str) or not field for field in field_names):
            raise ConfigurationError("pagination contract fields must be non-empty strings")
        if self.max_pages < 1:
            raise ConfigurationError("pagination contract max_pages must be positive")
        if self.mode == "CURSOR":
            if (
                not self.next_cursor_response_field
                or not self.cursor_request_parameter
                or self.current_page_response_field is not None
                or self.page_request_parameter is not None
                or self.start_page is not None
            ):
                raise ConfigurationError("cursor pagination contract was inconsistent")
        elif self.mode == "PAGE_NUMBER":
            if (
                self.next_cursor_response_field is not None
                or self.cursor_request_parameter is not None
                or not self.current_page_response_field
                or not self.page_request_parameter
                or not isinstance(self.start_page, int)
                or isinstance(self.start_page, bool)
                or self.start_page < 1
            ):
                raise ConfigurationError("page pagination contract was inconsistent")
        else:
            raise ConfigurationError("pagination contract mode was invalid")


@dataclass(frozen=True, slots=True)
class CollectionResult:
    data: dict[str, Any]
    ids: frozenset[str]
    total: int
    page_count: int
    authority: CompletenessAuthority
    pagination_mode: PaginationMode


@dataclass(frozen=True, slots=True)
class _PageState:
    total: int
    has_more: bool
    next_cursor: str | int | None
    page_number: int | None


PageFetcher = Callable[[Mapping[str, object]], Awaitable[Mapping[str, Any]]]
EntityId = Callable[[Mapping[str, Any]], str]


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApiResponseError(f"{label}: expected an object")
    return value


def _items(data: Mapping[str, Any], *, list_field: str, label: str) -> Sequence[object]:
    value = data.get(list_field)
    if not isinstance(value, list):
        raise ApiResponseError(f"{label}: expected {list_field} to be a list")
    return value


def _normalize_pagination_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value.casefold() if character.isalnum())


def _pagination_signal_keys(data: Mapping[str, Any]) -> set[str]:
    return {
        normalized
        for key in data
        if (normalized := _normalize_pagination_key(key)) in _PAGINATION_SIGNAL_FIELDS
    }


def _top_level_pagination_signals(data: Mapping[str, Any]) -> set[str]:
    return _pagination_signal_keys(data)


def _nested_pagination_signals(data: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for key, value in data.items():
        normalized = _normalize_pagination_key(key)
        if normalized not in _PAGINATION_CONTAINER_FIELDS:
            continue
        result.add(normalized)
        if isinstance(value, Mapping):
            result.update(_pagination_signal_keys(value))
    return result


def _has_any_pagination_signal(data: Mapping[str, Any]) -> bool:
    return bool(_top_level_pagination_signals(data) or _nested_pagination_signals(data))


def _pagination_container(
    data: Mapping[str, Any],
    *,
    contract: PaginationContract,
    label: str,
) -> Mapping[str, Any]:
    top_signals = _top_level_pagination_signals(data)
    if contract.response_container_path:
        allowed_top = {_normalize_pagination_key(contract.response_container_path[0])}
        if top_signals - allowed_top:
            raise SnapshotConsistencyError(f"{label}: pagination container signals conflicted")
    elif top_signals & _PAGINATION_CONTAINER_FIELDS:
        raise SnapshotConsistencyError(f"{label}: pagination container signals conflicted")

    container: Mapping[str, Any] = data
    for component in contract.response_container_path:
        if component not in container:
            raise SnapshotConsistencyError(f"{label}: pagination disappeared")
        container = _mapping(
            container[component],
            label=f"{label} pagination container",
        )
    return container


def _required_int(
    container: Mapping[str, Any],
    field: str,
    *,
    label: str,
    minimum: int,
) -> int:
    value = container.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ApiResponseError(f"{label}: pagination {field} was invalid")
    return value


def _required_bool(
    container: Mapping[str, Any],
    field: str,
    *,
    label: str,
) -> bool:
    value = container.get(field)
    if not isinstance(value, bool):
        raise ApiResponseError(f"{label}: pagination {field} was invalid")
    return value


def _cursor_value(value: object, *, label: str) -> str | int:
    if isinstance(value, bool) or not isinstance(value, str | int) or value == "":
        raise SnapshotConsistencyError(f"{label}: pagination cursor was invalid")
    try:
        hash(value)
    except TypeError as error:
        raise SnapshotConsistencyError(f"{label}: pagination cursor was not hashable") from error
    return value


def _page_state(
    data: Mapping[str, Any],
    *,
    contract: PaginationContract,
    label: str,
    expected_page: int | None,
) -> _PageState:
    container = _pagination_container(data, contract=contract, label=label)
    expected_fields = {contract.total_field, contract.has_more_field}
    if contract.mode == "CURSOR":
        assert contract.next_cursor_response_field is not None
        expected_fields.add(contract.next_cursor_response_field)
    else:
        assert contract.current_page_response_field is not None
        expected_fields.add(contract.current_page_response_field)
    normalized_expected_fields = {_normalize_pagination_key(field) for field in expected_fields}
    unexpected = _pagination_signal_keys(container) - normalized_expected_fields
    if unexpected:
        raise SnapshotConsistencyError(f"{label}: pagination mode contained an unverified field")

    if contract.total_field not in container:
        raise SnapshotConsistencyError(f"{label}: paginated response omitted total")
    if contract.has_more_field not in container:
        raise SnapshotConsistencyError(f"{label}: pagination has_more disappeared")
    total = _required_int(
        container,
        contract.total_field,
        label=label,
        minimum=0,
    )
    has_more = _required_bool(container, contract.has_more_field, label=label)
    next_cursor: str | int | None = None
    page_number: int | None = None
    if contract.mode == "CURSOR":
        cursor_field = contract.next_cursor_response_field
        assert cursor_field is not None
        raw_cursor = container.get(cursor_field)
        if has_more:
            next_cursor = _cursor_value(raw_cursor, label=label)
        elif raw_cursor is not None and raw_cursor != "":
            raise SnapshotConsistencyError(
                f"{label}: pagination cursor remained after the final page"
            )
    else:
        page_field = contract.current_page_response_field
        assert page_field is not None
        raw_page = container.get(page_field)
        if not isinstance(raw_page, int) or isinstance(raw_page, bool) or raw_page < 1:
            raise SnapshotConsistencyError(f"{label}: pagination page sequence was invalid")
        page_number = raw_page
        if expected_page is None or page_number != expected_page:
            raise SnapshotConsistencyError(f"{label}: pagination page sequence was invalid")
    return _PageState(total, has_more, next_cursor, page_number)


def _page_fingerprint(ids: Sequence[str]) -> str:
    encoded = json.dumps(ids, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def collect_complete_collection(
    fetch_page: PageFetcher,
    *,
    label: str,
    list_field: str,
    entity_id: EntityId,
    single_response_authority: CompletenessAuthority,
    pagination_contract: PaginationContract | None,
) -> CollectionResult:
    """Collect a proven single response or pages governed by an exact contract."""

    combined: list[object] = []
    unique_ids: set[str] = set()
    seen_cursors: set[str | int] = set()
    seen_page_fingerprints: set[str] = set()
    first_data: dict[str, Any] | None = None
    expected_total: int | None = None
    expected_page = (
        pagination_contract.start_page
        if pagination_contract is not None and pagination_contract.mode == "PAGE_NUMBER"
        else None
    )
    params: dict[str, object] = {}
    if pagination_contract is not None and pagination_contract.mode == "PAGE_NUMBER":
        assert pagination_contract.page_request_parameter is not None
        assert expected_page is not None
        params[pagination_contract.page_request_parameter] = expected_page
    maximum_pages = pagination_contract.max_pages if pagination_contract else 1

    for page_index in range(1, maximum_pages + 1):
        data = dict(await fetch_page(params))
        if first_data is None:
            first_data = data
        raw_items = _items(data, list_field=list_field, label=label)
        page_ids: list[str] = []
        for raw_item in raw_items:
            item = _mapping(raw_item, label=f"{label} item")
            identifier = entity_id(item)
            if identifier in unique_ids:
                raise SnapshotConsistencyError(f"{label}: repeated an entity across pages")
            unique_ids.add(identifier)
            page_ids.append(identifier)
            combined.append(raw_item)
        fingerprint = _page_fingerprint(page_ids)
        if fingerprint in seen_page_fingerprints:
            raise SnapshotConsistencyError(f"{label}: repeated a page")
        seen_page_fingerprints.add(fingerprint)

        if pagination_contract is None:
            if _has_any_pagination_signal(data):
                raise UnverifiedPaginationContract(
                    f"{label}: response exposed an unverified pagination contract"
                )
            first_data[list_field] = combined
            return CollectionResult(
                data=first_data,
                ids=frozenset(unique_ids),
                total=len(unique_ids),
                page_count=1,
                authority=single_response_authority,
                pagination_mode="SINGLE_RESPONSE",
            )

        state = _page_state(
            data,
            contract=pagination_contract,
            label=label,
            expected_page=expected_page,
        )
        if expected_total is None:
            expected_total = state.total
        elif state.total != expected_total:
            raise SnapshotConsistencyError(f"{label}: pagination total changed")
        if len(unique_ids) > expected_total:
            raise SnapshotConsistencyError(f"{label}: unique entity count exceeded total")

        reached_total = len(unique_ids) == expected_total
        if state.has_more and reached_total:
            raise SnapshotConsistencyError(f"{label}: has_more remained true at total")
        if not state.has_more and not reached_total:
            raise SnapshotConsistencyError(f"{label}: has_more ended before total")
        if reached_total:
            first_data[list_field] = combined
            return CollectionResult(
                data=first_data,
                ids=frozenset(unique_ids),
                total=expected_total,
                page_count=page_index,
                authority=(
                    "EXPLICIT_SINGLE_RESPONSE_TOTAL" if page_index == 1 else "EXPLICIT_PAGED_TOTAL"
                ),
                pagination_mode=pagination_contract.mode,
            )
        if not raw_items:
            raise SnapshotConsistencyError(f"{label}: empty page appeared before total")

        if pagination_contract.mode == "CURSOR":
            assert state.next_cursor is not None
            if state.next_cursor in seen_cursors:
                raise SnapshotConsistencyError(f"{label}: pagination cursor loop")
            seen_cursors.add(state.next_cursor)
            cursor_parameter = pagination_contract.cursor_request_parameter
            assert cursor_parameter is not None
            params = {cursor_parameter: state.next_cursor}
        else:
            assert expected_page is not None
            expected_page += 1
            page_parameter = pagination_contract.page_request_parameter
            assert page_parameter is not None
            params = {page_parameter: expected_page}

    raise SnapshotConsistencyError(f"{label}: maximum page count exceeded")
