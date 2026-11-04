"""Stage B RED tests: geometry validator + property persistence/fallback form/export.

TDD slice 4-5 backend. Run with:
  ./.venv/bin/python manage.py test portal.test_geometry
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from portal.geometry import (
    validate_polygon_geojson,
    validate_property_point,
    estimate_polygon_area_hectares,
    polygon_bounds,
)
from portal.models import Farm, FarmLocation, FarmProperty

User = get_user_model()
PW = "TesztJelszo123!"


def sq(lon0=20.0, lat0=46.0, s=0.01):
    ring = [[lon0, lat0], [lon0 + s, lat0], [lon0 + s, lat0 + s],
            [lon0, lat0 + s], [lon0, lat0]]
    return {"type": "Polygon", "coordinates": [ring]}


def make_farm_prop(**kw):
    u = User.objects.create_user(email=kw.pop("email", "g@example.com"), password=PW)
    f = Farm.objects.create(owner=u, name="F")
    loc = FarmLocation.objects.create(farm=f, name="L", latitude="46.250000",
                                      longitude="20.140000", timezone="Europe/Budapest")
    p = FarmProperty(farm=f, location=loc, name="P", kind="field", area_hectares="5.00")
    for k, v in kw.items():
        setattr(p, k, v)
    return u, f, loc, p


class PolygonValidatorTests(TestCase):
    def test_valid_closed_single_ring(self):
        ring = validate_polygon_geojson(sq())
        self.assertEqual(len(ring), 5)
        self.assertEqual(ring[0], ring[-1])

    def test_holes_rejected(self):
        bad = {"type": "Polygon", "coordinates": [sq()["coordinates"][0], sq()["coordinates"][0]]}
        with self.assertRaises(ValidationError):
            validate_polygon_geojson(bad)

    def test_multipolygon_feature_rejected(self):
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "MultiPolygon", "coordinates": []})
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Feature", "geometry": sq()})
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "LineString", "coordinates": [[20, 46], [21, 47]]})

    def test_open_ring_rejected(self):
        bad = {"type": "Polygon", "coordinates": [[[20, 46], [21, 46], [21, 47], [20, 47]]]}
        with self.assertRaises(ValidationError):
            validate_polygon_geojson(bad)

    def test_too_few_distinct_rejected(self):
        bad = {"type": "Polygon", "coordinates": [[[20, 46], [20, 46], [20, 46], [20, 46]]]}
        with self.assertRaises(ValidationError):
            validate_polygon_geojson(bad)

    def test_consecutive_duplicate_rejected(self):
        ring = [[20, 46], [21, 46], [21, 46], [20, 47], [20, 46]]
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Polygon", "coordinates": [ring]})

    def test_repeated_nonconsecutive_rejected(self):
        ring = [[20, 46], [21, 46], [21, 47], [20, 46], [20.5, 46.5], [20, 46]]
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Polygon", "coordinates": [ring]})

    def test_collinear_zero_area_rejected(self):
        ring = [[20, 46], [21, 46], [22, 46], [20, 46]]
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Polygon", "coordinates": [ring]})

    def test_bowtie_self_crossing_rejected(self):
        ring = [[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Polygon", "coordinates": [ring]})

    def test_self_touching_rejected(self):
        ring = [[0, 0], [3, 0], [3, 3], [1, 3], [1, 1], [2, 1], [2, 2], [0, 2], [0, 0]]
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Polygon", "coordinates": [ring]})

    def test_string_bool_nan_inf_rejected(self):
        base = sq()["coordinates"][0]
        for evil in ("20", True, float("nan"), float("inf"), None):
            ring = [list(pt) for pt in base]
            ring[1][0] = evil
            with self.assertRaises(ValidationError, msg=repr(evil)):
                validate_polygon_geojson({"type": "Polygon", "coordinates": [ring]})

    def test_out_of_range_rejected(self):
        ring = [[20, 46], [21, 46], [21, 95], [20, 46]]
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Polygon", "coordinates": [ring]})
        ring = [[190, 46], [191, 46], [191, 47], [190, 46]]
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Polygon", "coordinates": [ring]})

    def test_antimeridian_rejected(self):
        ring = [[179, 0], [-179, 0], [-179, 1], [179, 0]]
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Polygon", "coordinates": [ring]})

    def test_oversized_ring_rejected(self):
        import math
        pts = []
        for i in range(201):
            a = 2 * math.pi * i / 201
            pts.append([20 + 0.01 * math.cos(a), 46 + 0.01 * math.sin(a)])
        pts.append(list(pts[0]))
        with self.assertRaises(ValidationError):
            validate_polygon_geojson({"type": "Polygon", "coordinates": [pts]})

    def test_oversized_raw_json_rejected(self):
        with self.assertRaises(ValidationError):
            validate_polygon_geojson("x" * (32 * 1024 + 1))

    def test_area_estimate_plausible_and_missing_none(self):
        ha = estimate_polygon_area_hectares(sq()["coordinates"][0])
        self.assertGreater(ha, 50)
        self.assertLess(ha, 150)
        self.assertIsNone(estimate_polygon_area_hectares(None))

    def test_bounds(self):
        b = polygon_bounds(sq()["coordinates"][0])
        self.assertEqual(b, (20.0, 46.0, 20.01, 46.01))


class PointValidatorTests(TestCase):
    def test_valid_point(self):
        lat, lon = validate_property_point("46.25", "20.14")
        self.assertAlmostEqual(float(lat), 46.25)
        self.assertAlmostEqual(float(lon), 20.14)

    def test_empty_means_none(self):
        self.assertEqual(validate_property_point("", ""), (None, None))
        self.assertEqual(validate_property_point(None, None), (None, None))

    def test_half_point_rejected(self):
        with self.assertRaises(ValidationError):
            validate_property_point("46.25", "")
        with self.assertRaises(ValidationError):
            validate_property_point("", "20.14")

    def test_out_of_range_rejected(self):
        with self.assertRaises(ValidationError):
            validate_property_point("91", "20")
        with self.assertRaises(ValidationError):
            validate_property_point("46", "181")


class PropertyPersistenceTests(TestCase):
    def test_polygon_and_point_save(self):
        u, f, loc, p = make_farm_prop(boundary_geojson=sq(),
                                      point_latitude="46.255000", point_longitude="20.145000")
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.boundary_geojson["type"], "Polygon")
        self.assertEqual(str(p.area_hectares), "5.00")
        self.assertIsNotNone(p.map_area_hectares)
        loc.refresh_from_db()
        self.assertEqual(str(loc.latitude), "46.250000")

    def test_polygon_without_point_allowed(self):
        u, f, loc, p = make_farm_prop(boundary_geojson=sq())
        p.save()
        self.assertIsNotNone(p.map_area_hectares)

    def test_point_without_polygon_allowed(self):
        u, f, loc, p = make_farm_prop(point_latitude="46.25", point_longitude="20.14")
        p.save()
        self.assertIsNone(p.map_area_hectares)

    def test_clearing_allowed(self):
        u, f, loc, p = make_farm_prop(boundary_geojson=sq(),
                                      point_latitude="46.25", point_longitude="20.14")
        p.save()
        p.boundary_geojson = None
        p.point_latitude = None
        p.point_longitude = None
        p.save()
        self.assertIsNone(p.map_area_hectares)

    def test_invalid_geojson_blocks_save(self):
        u, f, loc, p = make_farm_prop(
            boundary_geojson={"type": "MultiPolygon", "coordinates": []})
        with self.assertRaises(ValidationError):
            p.save()
        self.assertFalse(FarmProperty.objects.filter(pk=getattr(p, "pk", None)).exists())

    def test_stated_area_untouched_by_geometry(self):
        u, f, loc, p = make_farm_prop(boundary_geojson=sq())
        p.save()
        self.assertEqual(str(FarmProperty.objects.get(pk=p.pk).area_hectares), "5.00")


class PropertyFormTests(TestCase):
    def test_form_saves_polygon_and_point(self):
        from portal.domain_forms import PropertyForm
        import json
        u, f, loc, _ = make_farm_prop()
        form = PropertyForm({"location": str(loc.pk), "name": "P2", "kind": "field",
                             "area_hectares": "3.00", "notes": "",
                             "boundary_geojson": json.dumps(sq()),
                             "point_latitude": "46.25", "point_longitude": "20.14"},
                            farm=f)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        obj.farm = f
        obj.save()
        self.assertEqual(obj.boundary_geojson["type"], "Polygon")

    def test_form_invalid_geojson_rerenders_no_save(self):
        from portal.domain_forms import PropertyForm
        u, f, loc, _ = make_farm_prop(email="h@example.com")
        n0 = FarmProperty.objects.filter(farm=f).count()
        form = PropertyForm({"location": str(loc.pk), "name": "P3", "kind": "field",
                             "area_hectares": "3.00", "notes": "",
                             "boundary_geojson": '{"type":"MultiPolygon","coordinates":[]}',
                             "point_latitude": "", "point_longitude": ""},
                            farm=f)
        self.assertFalse(form.is_valid())
        self.assertEqual(FarmProperty.objects.filter(farm=f).count(), n0)

    def test_form_half_point_rejected(self):
        from portal.domain_forms import PropertyForm
        u, f, loc, _ = make_farm_prop(email="i@example.com")
        form = PropertyForm({"location": str(loc.pk), "name": "P4", "kind": "field",
                             "area_hectares": "3.00", "notes": "",
                             "boundary_geojson": "",
                             "point_latitude": "46.25", "point_longitude": ""},
                            farm=f)
        self.assertFalse(form.is_valid())
