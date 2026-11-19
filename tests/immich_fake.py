"""A small in-memory Immich server (respx) shaped like the 3.3.5 API the provider uses."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
import respx
from PIL import Image


def jpeg(size: tuple[int, int] = (640, 480), color: tuple[int, int, int] = (90, 140, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def heic(size: tuple[int, int] = (320, 240)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 60, 60)).save(buffer, format="HEIF", quality=85)
    return buffer.getvalue()


def asset(asset_id: str, **overrides: Any) -> dict[str, Any]:
    """An asset as returned by search and ``GET /assets/{id}``."""
    data: dict[str, Any] = {
        "id": asset_id,
        "type": "IMAGE",
        "originalFileName": f"{asset_id}.jpg",
        "originalMimeType": "image/jpeg",
        "checksum": f"sum-{asset_id}",
        "isFavorite": False,
        "isArchived": False,
        "isTrashed": False,
        "isEdited": False,
        "visibility": "timeline",
        "localDateTime": "2024-07-14T10:22:11.000Z",
        "width": 640,
        "height": 480,
        "exifInfo": {
            "city": "Lyon",
            "country": "France",
            "orientation": "1",
            "fileSizeInByte": 50_000,
        },
        "people": [],
    }
    data.update(overrides)
    return data


def face(
    person_id: str | None,
    box: tuple[int, int, int, int],
    frame: tuple[int, int] = (640, 480),
    name: str = "",
) -> dict[str, Any]:
    return {
        "id": f"face-{person_id}-{box[0]}",
        "imageWidth": frame[0],
        "imageHeight": frame[1],
        "boundingBoxX1": box[0],
        "boundingBoxY1": box[1],
        "boundingBoxX2": box[2],
        "boundingBoxY2": box[3],
        "person": {"id": person_id, "name": name} if person_id else None,
    }


class FakeImmich:
    """State plus request handlers. ``calls`` records ``(method, path, query/body)``."""

    def __init__(self, base: str = "http://immich.test", key: str = "good-key") -> None:
        self.base = base
        self.key = key
        self.people: list[dict[str, Any]] = []
        self.person_thumbnails: dict[str, bytes] = {}
        self.albums: list[dict[str, Any]] = []  # each: {"id", "albumName", "assetIds": [...]}
        self.assets: dict[str, dict[str, Any]] = {}
        self.faces: dict[str, list[dict[str, Any]]] = {}
        self.files: dict[tuple[str, str], bytes | int] = {}  # (asset id, variant) -> body|status
        self.smart_results: dict[str, list[str]] = {}
        self.people_page_size = 500
        self.calls: list[tuple[str, str, Any]] = []
        self.search_error: int | None = None
        self.faces_error: int | None = None

    # --- fixtures ----------------------------------------------------------------------
    def add_person(self, person_id: str, name: str, hidden: bool = False) -> None:
        self.people.append({"id": person_id, "name": name, "isHidden": hidden})

    def add_asset(
        self,
        asset_id: str,
        *,
        people: list[tuple[str, str]] = (),  # type: ignore[assignment]
        faces: list[dict[str, Any]] | None = None,
        original: bytes | int | None = None,
        thumbnail: bytes | int | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        overrides.setdefault("people", [{"id": pid, "name": name} for pid, name in people])
        data = asset(asset_id, **overrides)
        self.assets[asset_id] = data
        self.faces[asset_id] = faces or []
        self.files[(asset_id, "original")] = jpeg() if original is None else original
        self.files[(asset_id, "fullsize")] = jpeg((1440, 1080))
        self.files[(asset_id, "preview")] = jpeg((1440, 1080))
        if thumbnail is not None:  # without one the server answers 404 for the thumbnail
            self.files[(asset_id, "thumbnail")] = thumbnail
        return data

    def add_album(self, album_id: str, name: str, asset_ids: list[str]) -> None:
        self.albums.append({"id": album_id, "albumName": name, "assetIds": asset_ids})

    # --- request log helpers ---------------------------------------------------------------
    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [body for _, p, body in self.calls if p == path]

    def downloads(self) -> list[tuple[str, dict[str, str]]]:
        return [
            (p, q)
            for m, p, q in self.calls
            if m == "GET" and "/assets/" in p and (p.endswith(("/original", "/thumbnail")))
        ]

    # --- wiring ----------------------------------------------------------------------------------
    def mount(self, router: respx.MockRouter) -> None:
        router.route(host=httpx.URL(self.base).host).mock(side_effect=self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body if body is not None else query))
        if path == "/api/server/version":
            return httpx.Response(200, json={"major": 3, "minor": 3, "patch": 5})
        if request.headers.get("x-api-key") != self.key:
            return httpx.Response(401, json={"message": "Invalid API key"})
        if path == "/api/people":
            return self._people(query)
        if path.startswith("/api/people/") and path.endswith("/thumbnail"):
            person_id = path.split("/")[3]
            if person_id in self.person_thumbnails:
                return httpx.Response(200, content=self.person_thumbnails[person_id])
            return httpx.Response(404)
        if path == "/api/albums":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": a["id"],
                        "albumName": a["albumName"],
                        "assetCount": len(a["assetIds"]),
                        "shared": False,
                        "albumThumbnailAssetId": a["assetIds"][0] if a["assetIds"] else None,
                    }
                    for a in self.albums
                ],
            )
        if path == "/api/search/metadata":
            return self._metadata(body)
        if path in {"/api/search/random", "/api/search/smart"}:
            return self._search(path, body)
        if path == "/api/faces":
            if self.faces_error is not None:
                return httpx.Response(self.faces_error)
            return httpx.Response(200, json=self.faces.get(query["id"], []))
        if path.startswith("/api/assets/"):
            return self._asset(path, query)
        return httpx.Response(404)

    def _people(self, query: dict[str, str]) -> httpx.Response:
        page = int(query.get("page", "1"))
        size = self.people_page_size
        chunk = self.people[(page - 1) * size : page * size]
        return httpx.Response(
            200,
            json={
                "people": chunk,
                "hasNextPage": page * size < len(self.people),
                "total": len(self.people),
                "hidden": 0,
            },
        )

    def _matches(self, data: dict[str, Any], body: dict[str, Any]) -> bool:
        if body.get("isFavorite") and not data["isFavorite"]:
            return False
        if body.get("visibility") == "timeline" and data["isArchived"]:
            return False
        person_ids = body.get("personIds") or []
        present = {p["id"] for p in data["people"]}
        if person_ids and not set(person_ids) <= present:
            return False
        album_ids = body.get("albumIds") or []
        if album_ids:
            members = {aid for a in self.albums if a["id"] in album_ids for aid in a["assetIds"]}
            if data["id"] not in members:
                return False
        taken = datetime.fromisoformat(data["localDateTime"].replace("Z", "+00:00"))
        after, before = body.get("takenAfter"), body.get("takenBefore")
        if after and taken < datetime.fromisoformat(after.replace("Z", "+00:00")):
            return False
        if before and taken > datetime.fromisoformat(before.replace("Z", "+00:00")):
            return False
        return not data["isTrashed"]

    def _search(self, path: str, body: dict[str, Any]) -> httpx.Response:
        if self.search_error is not None:
            return httpx.Response(self.search_error)
        pool = [a for a in self.assets.values() if self._matches(a, body)]
        if path == "/api/search/smart":
            ranked = self.smart_results.get(body["query"])
            if ranked is not None:
                pool = [self.assets[i] for i in ranked if i in {a["id"] for a in pool}]
            items = pool[: body["size"]]
            return httpx.Response(
                200, json={"albums": {}, "assets": {"items": items, "count": len(items)}}
            )
        return httpx.Response(200, json=pool[: body["size"]])

    def _metadata(self, body: dict[str, Any]) -> httpx.Response:
        """Paged asset listing (used for album membership); ignores archive/lock filters."""
        members = {
            asset_id
            for album in self.albums
            if album["id"] in (body.get("albumIds") or [])
            for asset_id in album["assetIds"]
        }
        items = [a for a in self.assets.values() if a["id"] in members]
        size, page = body["size"], body["page"]
        chunk = items[(page - 1) * size : page * size]
        next_page = str(page + 1) if page * size < len(items) else None
        return httpx.Response(
            200, json={"assets": {"items": chunk, "count": len(chunk), "nextPage": next_page}}
        )

    def _asset(self, path: str, query: dict[str, str]) -> httpx.Response:
        parts = path.split("/")  # ['', 'api', 'assets', id, (original|thumbnail)?]
        asset_id = parts[3]
        if asset_id not in self.assets:
            return httpx.Response(404)
        if len(parts) == 4:
            return httpx.Response(200, json=self.assets[asset_id])
        variant = "original" if parts[4] == "original" else query.get("size", "preview")
        content = self.files.get((asset_id, variant), 404)
        if isinstance(content, int):
            return httpx.Response(content)
        return httpx.Response(200, content=content, headers={"content-type": "image/jpeg"})


Mounter = Callable[[FakeImmich], FakeImmich]
