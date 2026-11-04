"""Stage B pure-Python WGS84 geometry validation (no GIS dependencies).

Single Polygon ring in [lon, lat] order, plus a wholly separate nullable
property point. All validation is server-side; the browser map is only
an enhancement over the fallback form fields.
"""
import json
import math
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MAX_POSITIONS = 200
MAX_RAW_BYTES = 32 * 1024
EARTH_RADIUS_M = 6371000.0


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _segments_cross(p1, p2, p3, p4):
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_seg(a, b, c):
        return (min(a[0], c[0]) <= b[0] <= max(a[0], c[0])
                and min(a[1], c[1]) <= b[1] <= max(a[1], c[1]))

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    if d1 == 0 and on_seg(p3, p1, p4):
        return True
    if d2 == 0 and on_seg(p3, p2, p4):
        return True
    if d3 == 0 and on_seg(p1, p3, p2):
        return True
    if d4 == 0 and on_seg(p1, p4, p2):
        return True
    return False


def validate_polygon_geojson(value):
    """Validate stored GeoJSON Polygon. Return the ring on success.

    Accepts a dict or a raw JSON string (bounded to 32 KiB before decode,
    non-finite constants rejected). Raises ValidationError otherwise.
    """
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_RAW_BYTES:
            raise ValidationError(_("A polygon JSON túl nagy (max. 32 KiB)."))
        try:
            value = json.loads(value, parse_constant=_reject_constant)
        except ValidationError:
            raise
        except Exception:
            raise ValidationError(_("Érvénytelen polygon GeoJSON."))
    if not isinstance(value, dict):
        raise ValidationError(_("Érvénytelen polygon GeoJSON."))
    if value.get("type") != "Polygon":
        raise ValidationError(_("Csak egyetlen Polygon típusú geometria menthető."))
    coords = value.get("coordinates")
    if not isinstance(coords, list) or len(coords) != 1 or not isinstance(coords[0], list):
        raise ValidationError(_("A Polygon pontosan egy zárt gyűrűt tartalmazhat."))
    ring = coords[0]
    if len(ring) < 4:
        raise ValidationError(_("A gyűrű legalább 4 pozícióból álljon."))
    if len(ring) > MAX_POSITIONS:
        raise ValidationError(_("A gyűrű legfeljebb 200 pozícióból állhat."))
    pts = []
    for pt in ring:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValidationError(_("Minden koordináta pontosan két szám legyen."))
        lon, lat = pt
        if not _is_num(lon) or not _is_num(lat):
            raise ValidationError(_("A koordináták véges számok legyenek."))
        lon, lat = float(lon), float(lat)
        if not (-180 <= lon <= 180):
            raise ValidationError(_("A hosszúság -180 és 180 között legyen."))
        if not (-90 <= lat <= 90):
            raise ValidationError(_("A szélesség -90 és 90 között legyen."))
        pts.append([lon, lat])
    if pts[0] != pts[-1]:
        raise ValidationError(_("A gyűrű legyen zárt (első = utolsó pont)."))
    body = pts[:-1]
    if len({(p[0], p[1]) for p in body}) < 3:
        raise ValidationError(_("A gyűrű legalább 3 különböző csúcsot tartalmazzon."))
    for i in range(len(body)):
        if body[i] == body[(i + 1) % len(body)]:
            raise ValidationError(_("Egymást követő duplikált pont nem megengedett."))
    seen = set()
    for p in body:
        key = (p[0], p[1])
        if key in seen:
            raise ValidationError(_("Ismétlődő csúcs nem megengedett."))
        seen.add(key)
    lons = [p[0] for p in pts]
    if max(lons) - min(lons) > 180:
        raise ValidationError(_("A polygon nem nyúlhat át az antimeridiánon."))
    for i in range(len(pts) - 1):
        if abs(pts[i + 1][0] - pts[i][0]) > 180:
            raise ValidationError(_("A polygon nem nyúlhat át az antimeridiánon."))
    area2 = sum(pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
                for i in range(len(pts) - 1))
    if area2 == 0:
        raise ValidationError(_("A polygon területe nem lehet nulla (kollineáris pontok)."))
    segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    m = len(segs)
    for i in range(m):
        for j in range(i + 1, m):
            if j == i + 1 or (i == 0 and j == m - 1):
                continue
            if _segments_cross(segs[i][0], segs[i][1], segs[j][0], segs[j][1]):
                raise ValidationError(_("A polygon nem metszheti önmagát."))
    return pts


def _reject_constant(name):
    raise ValidationError(_("A koordináták véges számok legyenek."))


def validate_property_point(latitude, longitude):
    """Validate the separate nullable property point.

    Returns (Decimal|None, Decimal|None); both empty means no point,
    exactly one empty is rejected. Raises ValidationError otherwise.
    """
    empty = (v is None or (isinstance(v, str) and not v.strip()) for v in (latitude, longitude))
    lat_empty, lon_empty = empty
    if lat_empty and lon_empty:
        return (None, None)
    if lat_empty or lon_empty:
        raise ValidationError(_("A ponthoz szélesség és hosszúság együtt kell."))
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        raise ValidationError(_("Érvénytelen koordináta."))
    try:
        lat = Decimal(str(latitude).strip())
        lon = Decimal(str(longitude).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValidationError(_("Érvénytelen koordináta."))
    if not lat.is_finite() or not lon.is_finite():
        raise ValidationError(_("A koordináták véges számok legyenek."))
    if not (-90 <= lat <= 90):
        raise ValidationError(_("A szélesség -90 és 90 között legyen."))
    if not (-180 <= lon <= 180):
        raise ValidationError(_("A hosszúság -180 és 180 között legyen."))
    return (lat, lon)


def estimate_polygon_area_hectares(ring):
    """Equirectangular estimate, centred on the ring. None when no polygon."""
    if ring is None:
        return None
    if isinstance(ring, dict):
        ring = validate_polygon_geojson(ring)
    if not ring:
        return None
    mean_lat = sum(p[1] for p in ring) / len(ring)
    cos_lat = math.cos(math.radians(mean_lat))
    xs = [EARTH_RADIUS_M * math.radians(p[0]) * cos_lat for p in ring]
    ys = [EARTH_RADIUS_M * math.radians(p[1]) for p in ring]
    area2 = sum(xs[i] * ys[i + 1] - xs[i + 1] * ys[i] for i in range(len(ring) - 1))
    return abs(area2) / 2 / 10000.0


def polygon_bounds(ring):
    """(min_lon, min_lat, max_lon, max_lat) or None when no polygon."""
    if ring is None:
        return None
    if isinstance(ring, dict):
        ring = validate_polygon_geojson(ring)
    if not ring:
        return None
    return (min(p[0] for p in ring), min(p[1] for p in ring),
            max(p[0] for p in ring), max(p[1] for p in ring))
