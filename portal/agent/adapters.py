"""Explicit CRUD adapters for registry targets beyond the MVP three.

Each adapter is an explicit descriptor: allowed field allowlist, allowed
roles per op, farm- or owner-scoped queryset, validation through the real
forms and model full_clean, and a delete-impact check before any write.
No raw model-registry introspection is used: every target maps to named
models, typed IDs and the same forms the manual views use.

Covered targets (20): farm, extra_field, location, property,
crop_cycle, observation, external_contact, rule_category,
rule_definition, rule_activation, equipment_property, livestock_group,
livestock_event, livestock_work, farming_event, farm_document,
news_source, membership, invitation, document. Forbidden operations
fail closed with a precise reason instead of a fake full CRUD.
"""
from django.core.exceptions import PermissionDenied, ValidationError

NEW_TARGETS = (
    "farm",
    "extra_field",
    "location",
    "property",
    "crop_cycle",
    "observation",
    "external_contact",
    "rule_category",
    "rule_definition",
    "rule_activation",
    "equipment_property",
    "livestock_group",
    "livestock_event",
    "livestock_work",
    "farming_event",
    "farm_document",
    "news_source",
    "membership",
    "invitation",
    "document",
)

_FARM_FIELDS = frozenset({
    "name", "region_text", "climate_text", "farming_direction_text",
    "soil_notes", "water_notes", "crops_livestock_notes",
    "equipment_labour_notes", "goals_notes", "constraints_notes"})
_EXTRA_FIELDS = frozenset({"key", "value"})
_LOCATION_FIELDS = frozenset({"name", "latitude", "longitude", "timezone"})
_PROPERTY_FIELDS = frozenset({
    "location", "name", "kind", "area_hectares", "notes",
    "boundary_geojson", "point_latitude", "point_longitude"})
_CYCLE_FIELDS = frozenset({
    "property", "crop_name", "growth_stage", "sow_date",
    "expected_end_date", "observations"})
_OBSERVATION_FIELDS = frozenset({"observation"})
_CONTACT_FIELDS = frozenset({
    "name", "phone", "email", "services", "notes", "availability"})
_RULE_CATEGORY_FIELDS = frozenset({"name"})
_RULE_DEFINITION_FIELDS = frozenset({
    "name", "category", "body", "is_public", "new_category"})
_RULE_ACTIVATION_FIELDS = frozenset({"rule", "is_active"})
_EQUIPMENT_PROPERTY_FIELDS = frozenset({"equipment", "property"})
_LIVESTOCK_GROUP_FIELDS = frozenset({
    "name", "species", "breed_label", "headcount", "housing_property",
    "notes", "is_active"})
_LIVESTOCK_EVENT_FIELDS = frozenset({
    "group", "event_date", "kind", "headcount_delta", "target_group",
    "property", "notes", "create_journal", "fact_ack", "fact_statement"})
_LIVESTOCK_WORK_FIELDS = frozenset({
    "livestock_group", "livestock_kind", "title", "start_date",
    "end_date", "estimated_hours", "property", "notes"})
_FARMING_EVENT_FIELDS = frozenset({
    "property", "crop_cycle", "crop_name_snapshot", "event_date",
    "event_type", "description", "quantity", "unit", "materials",
    "treated_area_hectares", "notes", "fact_ack"})
_FARM_DOCUMENT_FIELDS = frozenset({"document", "is_active"})
_NEWS_SOURCE_FIELDS = frozenset({"name", "url", "notes", "is_active"})
_MEMBERSHIP_FIELDS = frozenset({"role", "is_active"})
_INVITATION_FIELDS = frozenset({"email", "role", "revoked"})
_DOCUMENT_FIELDS = frozenset({"name", "description", "is_public"})

FIELDS = {
    "farm": _FARM_FIELDS,
    "extra_field": _EXTRA_FIELDS,
    "location": _LOCATION_FIELDS,
    "property": _PROPERTY_FIELDS,
    "crop_cycle": _CYCLE_FIELDS,
    "observation": _OBSERVATION_FIELDS,
    "external_contact": _CONTACT_FIELDS,
    "rule_category": _RULE_CATEGORY_FIELDS,
    "rule_definition": _RULE_DEFINITION_FIELDS,
    "rule_activation": _RULE_ACTIVATION_FIELDS,
    "equipment_property": _EQUIPMENT_PROPERTY_FIELDS,
    "livestock_group": _LIVESTOCK_GROUP_FIELDS,
    "livestock_event": _LIVESTOCK_EVENT_FIELDS,
    "livestock_work": _LIVESTOCK_WORK_FIELDS,
    "farming_event": _FARMING_EVENT_FIELDS,
    "farm_document": _FARM_DOCUMENT_FIELDS,
    "news_source": _NEWS_SOURCE_FIELDS,
    "membership": _MEMBERSHIP_FIELDS,
    "invitation": _INVITATION_FIELDS,
    "document": _DOCUMENT_FIELDS,
}

# Scope of the row ownership check: "farm" rows belong to the active
# farm, "owner" rows belong to the acting user, "farm_object" is the
# single Farm row itself.
SCOPE = {
    "farm": "farm_object",
    "extra_field": "farm",
    "location": "farm",
    "property": "farm",
    "crop_cycle": "farm",
    "observation": "farm",
    "external_contact": "farm",
    "rule_category": "owner",
    "rule_definition": "owner",
    "rule_activation": "farm",
    "equipment_property": "farm",
    "livestock_group": "farm",
    "livestock_event": "farm",
    "livestock_work": "farm",
    "farming_event": "farm",
    "farm_document": "farm",
    "news_source": "owner",
    "membership": "farm",
    "invitation": "farm",
    "document": "owner",
}

# Allowed farm roles per op; owner-scoped targets additionally require
# the row owner to equal the actor (checked per row, not per role).
ROLES = {
    "farm": {"create": ("owner",), "update": ("owner",),
             "delete": ("owner",)},
    "extra_field": {"create": ("owner", "manager"),
                    "update": ("owner", "manager"),
                    "delete": ("owner", "manager")},
    "location": {"create": ("owner", "manager"),
                 "update": ("owner", "manager"),
                 "delete": ("owner", "manager")},
    "property": {"create": ("owner", "manager"),
                 "update": ("owner", "manager"),
                 "delete": ("owner", "manager")},
    "crop_cycle": {"create": ("owner", "manager"),
                   "update": ("owner", "manager"),
                   "delete": ("owner", "manager")},
    # Observation create/delete pass the role gate so validate can
    # return the precise hard-block reason (update the cycle instead).
    "observation": {"create": ("owner", "manager", "worker"),
                    "update": ("owner", "manager", "worker"),
                    "delete": ("owner", "manager", "worker")},
    "external_contact": {"create": ("owner",),
                         "update": ("owner",),
                         "delete": ("owner",)},
    "rule_category": {"create": ("owner", "manager", "worker", "viewer"),
                      "update": ("owner", "manager", "worker", "viewer"),
                      "delete": ("owner", "manager", "worker", "viewer")},
    "rule_definition": {"create": ("owner", "manager", "worker", "viewer"),
                        "update": ("owner", "manager", "worker", "viewer"),
                        "delete": ("owner", "manager", "worker", "viewer")},
    "rule_activation": {"create": ("owner", "manager"),
                        "update": ("owner", "manager"),
                        "delete": ("owner", "manager")},
    "equipment_property": {"create": ("owner", "manager"),
                           "update": ("owner", "manager"),
                           "delete": ("owner", "manager")},
    "livestock_group": {"create": ("owner", "manager"),
                        "update": ("owner", "manager"),
                        "delete": ("owner", "manager")},
    "livestock_event": {"create": ("owner", "manager", "worker"),
                        "update": ("owner", "manager", "worker"),
                        "delete": ("owner", "manager", "worker")},
    "livestock_work": {"create": ("owner", "manager"),
                       "update": ("owner", "manager"),
                       "delete": ("owner", "manager")},
    "farming_event": {"create": ("owner", "manager", "worker"),
                      "update": ("owner", "manager"),
                      "delete": ("owner", "manager")},
    "farm_document": {"create": ("owner", "manager"),
                      "update": ("owner", "manager"),
                      "delete": ("owner", "manager")},
    "news_source": {"create": ("owner", "manager", "worker", "viewer"),
                    "update": ("owner", "manager", "worker", "viewer"),
                    "delete": ("owner", "manager", "worker", "viewer")},
    "membership": {"create": ("owner",),
                   "update": ("owner",),
                   "delete": ("owner",)},
    "invitation": {"create": ("owner",),
                   "update": ("owner",),
                   "delete": ("owner",)},
    "document": {"create": ("owner", "manager", "worker", "viewer"),
                 "update": ("owner", "manager", "worker", "viewer"),
                 "delete": ("owner", "manager", "worker", "viewer")},
}


def _role_of(actor, farm):
    from portal.permissions import role_for
    return role_for(actor, farm)


def _active_or_denied(actor):
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise PermissionDenied("Actor must be an authenticated user.")
    if not getattr(actor, "is_active", False):
        raise PermissionDenied("Actor is revoked or inactive.")


def check_permission(actor, farm, target, op):
    """Role gate for one adapter target and op."""
    _active_or_denied(actor)
    allowed = ROLES[target].get(op)
    if not allowed:
        raise PermissionDenied(
            f"Op {op!r} is not offered on {target}.")
    if SCOPE[target] == "owner":
        # Any farm role may manage only their own rows; the per-row
        # owner check in validate/execute enforces the boundary.
        role = _role_of(actor, farm)
        if role is None and getattr(farm, "owner_id", None) != actor.pk:
            raise PermissionDenied(
                f"Role None may not write {target}.")
        return role
    from django.core.exceptions import PermissionDenied as _PD
    role = _role_of(actor, farm)
    if role not in allowed:
        raise _PD(f"Role {role!r} may not write {target}.")
    return role


def _actor_or_owner(farm, actor):
    if actor is not None:
        return actor
    return getattr(farm, "owner", None)


def scoped_qs(target, farm, actor=None):
    """Explicit per-target queryset; owner rows scope to the actor."""
    from portal import models as m
    if target == "farm":
        return m.Farm.objects.filter(pk=farm.pk)
    if target == "extra_field":
        return m.FarmExtraField.objects.filter(farm=farm)
    if target == "location":
        return m.FarmLocation.objects.filter(farm=farm)
    if target == "property":
        return m.FarmProperty.objects.filter(farm=farm)
    if target in ("crop_cycle", "observation"):
        return m.CropCycle.objects.filter(farm=farm)
    if target == "external_contact":
        return m.ExternalContact.objects.filter(farm=farm)
    who = _actor_or_owner(farm, actor)
    if target == "rule_category":
        return m.RuleCategory.objects.filter(owner=who)
    if target == "rule_definition":
        return m.RuleDefinition.objects.filter(owner=who)
    if target == "rule_activation":
        return m.RuleActivation.objects.filter(farm=farm)
    if target == "equipment_property":
        return m.EquipmentProperty.objects.filter(
            equipment__farm=farm, property__farm=farm)
    if target == "livestock_group":
        return m.LivestockGroup.objects.filter(farm=farm)
    if target == "livestock_event":
        return m.LivestockEvent.objects.filter(farm=farm)
    if target == "livestock_work":
        return m.WorkItem.objects.filter(
            farm=farm, livestock_group__isnull=False)
    if target == "farming_event":
        return m.FarmingEvent.objects.filter(farm=farm)
    if target == "farm_document":
        return m.FarmDocument.objects.filter(farm=farm)
    if target == "membership":
        return m.Membership.objects.filter(farm=farm)
    if target == "invitation":
        return m.Invitation.objects.filter(farm=farm)
    who = _actor_or_owner(farm, actor)
    if target == "news_source":
        return m.NewsSource.objects.filter(owner=who)
    if target == "document":
        return m.Document.objects.filter(owner=who)
    raise ValueError(f"Unknown adapter target: {target!r}")


# ---------------------------------------------------------------------------
# Validation (dry run, writes nothing)
# ---------------------------------------------------------------------------

def validate(farm, cmd, actor=None):
    """Dry-run validation of one parsed new-target command."""
    target, op = cmd["target"], cmd["op"]
    if target == "farm":
        return _validate_farm(farm, cmd)
    if target == "extra_field":
        return _validate_extra(farm, cmd)
    if target == "location":
        return _validate_location(farm, cmd)
    if target == "property":
        return _validate_property(farm, cmd)
    if target == "crop_cycle":
        return _validate_cycle(farm, cmd)
    if target == "observation":
        return _validate_observation(farm, cmd)
    if target == "external_contact":
        return _validate_contact(farm, cmd)
    if target == "rule_category":
        return _validate_rule_category(farm, cmd, actor)
    if target == "rule_definition":
        return _validate_rule_definition(farm, cmd, actor)
    if target == "rule_activation":
        return _validate_rule_activation(farm, cmd, actor)
    if target == "equipment_property":
        return _validate_equipment_property(farm, cmd)
    if target == "livestock_group":
        return _validate_livestock_group(farm, cmd)
    if target == "livestock_event":
        return _validate_livestock_event(farm, cmd, actor)
    if target == "livestock_work":
        return _validate_livestock_work(farm, cmd)
    if target == "farming_event":
        return _validate_farming_event(farm, cmd, actor)
    if target == "farm_document":
        return _validate_farm_document(farm, cmd, actor)
    if target == "news_source":
        return _validate_news_source(farm, cmd, actor)
    if target == "membership":
        return _validate_membership(farm, cmd, actor)
    if target == "invitation":
        return _validate_invitation(farm, cmd, actor)
    if target == "document":
        return _validate_document_meta(farm, cmd, actor)
    raise ValueError(f"Unknown adapter target: {target!r}")


def _get_owned(farm, cmd, actor, what):
    try:
        return scoped_qs(cmd["target"], farm, actor).get(pk=cmd["id"])
    except Exception:
        raise ValidationError(f"{what} #{cmd['id']} is not available.")


def _validate_farm(farm, cmd):
    from portal.forms import FarmForm
    op = cmd["op"]
    if op == "create":
        raise ValidationError(
            "Farm create is not offered through changesets: farm rows "
            "are created only with the user account; use profile update.")
    if op == "delete":
        raise ValidationError(_farm_delete_reason(farm))
    obj = scoped_qs("farm", farm).filter(pk=cmd["id"]).first()
    if obj is None or obj.pk != farm.pk:
        raise ValidationError(
            f"Farm #{cmd['id']} is not the active farm.")
    data = {key: getattr(obj, key) for key in _FARM_FIELDS}
    for key, value in cmd["fields"].items():
        data[key] = value
    form = FarmForm(data=data, instance=obj)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.full_clean(exclude=[])
    return tmp


def _farm_delete_reason(farm):
    from portal import models as m
    counts = [
        m.FarmAction.objects.filter(farm=farm).count(),
        m.WorkItem.objects.filter(farm=farm).count(),
        m.FarmingEvent.objects.filter(farm=farm).count(),
        m.CropCycle.objects.filter(farm=farm).count(),
    ]
    total = sum(counts)
    base = ("Farm delete is not offered through changesets; "
            "use account deletion.")
    if total:
        return (f"{base} Protected history is present "
                f"({total} rows).")
    return base


def _validate_extra(farm, cmd):
    from portal.forms import FarmExtraFieldForm
    op = cmd["op"]
    if op == "create":
        form = FarmExtraFieldForm(data=dict(cmd["fields"]))
        obj = form.instance
        obj.farm = farm
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, None, "Extra field")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    data = {"key": obj.key, "value": obj.value}
    data.update(cmd["fields"])
    form = FarmExtraFieldForm(data=data, instance=obj)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.farm = farm
    tmp.full_clean()
    return tmp


def _validate_location(farm, cmd):
    from portal.domain_forms import LocationForm
    op = cmd["op"]
    if op == "create":
        form = LocationForm(data=dict(cmd["fields"]))
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.farm = farm
        obj.full_clean()
        return obj
    from portal import models as m
    obj = _get_owned(farm, cmd, None, "Location")
    if op == "delete":
        linked = m.FarmProperty.objects.filter(location=obj).count()
        if linked:
            raise ValidationError(
                f"Delete blocked: {linked} property rows reference "
                "this location.")
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    data = {"name": obj.name, "latitude": str(obj.latitude),
            "longitude": str(obj.longitude), "timezone": obj.timezone}
    for key, value in cmd["fields"].items():
        data[key] = value
    form = LocationForm(data=data, instance=obj)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.farm = farm
    tmp.full_clean()
    return tmp


def _property_form_data(fields, base=None):
    import json as _json
    if base is None:
        data = {}
    else:
        data = {"location": str(base.location_id), "name": base.name,
                "kind": base.kind,
                "area_hectares": str(base.area_hectares),
                "notes": base.notes or "",
                "boundary_geojson": "",
                "point_latitude": "", "point_longitude": ""}
        if base.boundary_geojson:
            try:
                data["boundary_geojson"] = _json.dumps(
                    base.boundary_geojson)
            except Exception:
                data["boundary_geojson"] = ""
        if base.point_latitude is not None:
            data["point_latitude"] = str(base.point_latitude)
        if base.point_longitude is not None:
            data["point_longitude"] = str(base.point_longitude)
    for key, value in fields.items():
        if key == "boundary_geojson" and isinstance(value, (dict, list)):
            data[key] = _json.dumps(value)
        elif value is None and key in ("notes", "boundary_geojson",
                                       "point_latitude",
                                       "point_longitude"):
            data[key] = ""
        else:
            data[key] = value
    return data


def _validate_property(farm, cmd):
    from portal import models as m
    from portal.domain_forms import PropertyForm
    op = cmd["op"]
    if "location" in cmd["fields"]:
        loc = m.FarmLocation.objects.filter(
            pk=cmd["fields"]["location"], farm=farm).first()
        if cmd["fields"]["location"] is not None and loc is None:
            raise ValidationError(
                {"location": "Cross-farm location is not allowed."})
    if op == "create":
        form = PropertyForm(data=_property_form_data(cmd["fields"]),
                            farm=farm)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.farm = farm
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, None, "Property")
    if op == "delete":
        cycles = m.CropCycle.objects.filter(property=obj).count()
        works = m.WorkItem.objects.filter(property=obj).count()
        events = m.FarmingEvent.objects.filter(property=obj).count()
        housed = m.LivestockGroup.objects.filter(
            housing_property=obj).count()
        total = cycles + works + events + housed
        if total:
            raise ValidationError(
                "Delete blocked: protected history references this "
                f"property (cycles={cycles}, work={works}, "
                f"events={events}, housed groups={housed}).")
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    form = PropertyForm(data=_property_form_data(cmd["fields"], obj),
                        instance=obj, farm=farm)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.farm = farm
    tmp.full_clean()
    return tmp


def _validate_cycle(farm, cmd):
    from portal import models as m
    from portal.domain_forms import CropCycleForm
    op = cmd["op"]
    if "property" in cmd["fields"]:
        prop = m.FarmProperty.objects.filter(
            pk=cmd["fields"]["property"], farm=farm).first()
        if cmd["fields"]["property"] is not None and prop is None:
            raise ValidationError(
                {"property": "Cross-farm property is not allowed."})
    if op == "create":
        form = CropCycleForm(data=dict(cmd["fields"]), farm=farm)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.farm = farm
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, None, "Crop cycle")
    if op == "delete":
        events = m.FarmingEvent.objects.filter(crop_cycle=obj).count()
        works = m.WorkItem.objects.filter(crop_cycle=obj).count()
        if events + works:
            raise ValidationError(
                "Delete blocked: protected history is linked "
                f"(events={events}, work items={works}).")
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    data = {"property": str(obj.property_id),
            "crop_name": obj.crop_name,
            "growth_stage": obj.growth_stage or "",
            "sow_date": obj.sow_date.isoformat()
            if obj.sow_date else "",
            "expected_end_date": obj.expected_end_date.isoformat()
            if obj.expected_end_date else "",
            "observations": obj.observations or ""}
    for key, value in cmd["fields"].items():
        data[key] = "" if value is None and key != "property" else value
    form = CropCycleForm(data=data, instance=obj, farm=farm)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    return form.instance


def _validate_observation(farm, cmd):
    from portal.domain_forms import ObservationForm
    op = cmd["op"]
    if op == "create":
        raise ValidationError(
            "Observation create is not offered; update the cycle "
            "observations instead.")
    if op == "delete":
        raise ValidationError(
            "Observation delete is not offered; update the cycle with "
            "an empty observation to clear it.")
    obj = _get_owned(farm, cmd, None, "Crop cycle")
    if "observation" not in cmd["fields"]:
        raise ValidationError(
            "Observation text is required (empty string clears it).")
    value = cmd["fields"]["observation"]
    if value is None:
        raise ValidationError(
            "Observation text is required (empty string clears it).")
    if str(value) == "":
        obj.observations = ""
    else:
        form = ObservationForm(data={"observation": value})
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj.observations = form.cleaned_data["observation"]
    obj.full_clean()
    return obj


def _validate_contact(farm, cmd):
    from portal.domain_forms import ExternalContactForm
    op = cmd["op"]
    if op == "create":
        form = ExternalContactForm(data=dict(cmd["fields"]))
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.farm = farm
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, None, "External contact")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    data = {"name": obj.name, "phone": obj.phone, "email": obj.email,
            "services": obj.services, "notes": obj.notes,
            "availability": obj.availability}
    data.update(cmd["fields"])
    form = ExternalContactForm(data=data, instance=obj)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.farm = farm
    tmp.full_clean()
    return tmp


def _validate_rule_category(farm, cmd, actor):
    from portal import models as m
    from portal.domain_forms import RuleCategoryForm
    who = _actor_or_owner(farm, actor)
    op = cmd["op"]
    if op == "create":
        form = RuleCategoryForm(data=dict(cmd["fields"]), owner=who)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.owner = who
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, actor, "Rule category")
    if obj.owner_id != who.pk:
        raise ValidationError("Rule category is not yours.")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    data = {"name": obj.name}
    data.update(cmd["fields"])
    form = RuleCategoryForm(data=data, instance=obj, owner=who)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.owner = who
    tmp.full_clean()
    return tmp


def _resolve_rule_category(who, fields):
    from portal import models as m
    if fields.get("new_category"):
        name = str(fields["new_category"]).strip()
        if not name:
            raise ValidationError(
                {"new_category": "Category name is required."})
        cat, _created = m.RuleCategory.objects.get_or_create(
            owner=who, name=name)
        return cat
    if "category" in fields and fields["category"] is not None:
        cat = m.RuleCategory.objects.filter(
            pk=fields["category"], owner=who).first()
        if cat is None:
            raise ValidationError(
                {"category": "Category is not yours."})
        return cat
    return None


def _validate_rule_definition(farm, cmd, actor):
    from portal import models as m
    from portal.domain_forms import RuleDefinitionForm
    who = _actor_or_owner(farm, actor)
    op = cmd["op"]
    if op == "create":
        new_name = cmd["fields"].get("new_category")
        if new_name:
            cat = _resolve_rule_category(who, cmd["fields"])
            data = dict(cmd["fields"])
            data.pop("category", None)
        else:
            cat = _resolve_rule_category(who, cmd["fields"])
            data = dict(cmd["fields"])
            data.pop("new_category", None)
            if cat is not None:
                data["category"] = str(cat.pk)
        form = RuleDefinitionForm(data=data, user=who)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.owner = who
        if new_name:
            obj.category = cat
        elif cat is not None:
            obj.category = cat
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, actor, "Rule definition")
    if obj.owner_id != who.pk:
        raise ValidationError("Rule definition is not yours.")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    data = {"name": obj.name,
            "category": str(obj.category_id)
            if obj.category_id else "",
            "body": obj.body,
            "is_public": obj.is_public}
    extra = dict(cmd["fields"])
    new_name = extra.pop("new_category", None)
    new_cat = None
    if new_name:
        new_cat = _resolve_rule_category(who, {"new_category": new_name})
        extra.pop("category", None)
    elif "category" in extra and extra["category"] is not None:
        cat = m.RuleCategory.objects.filter(
            pk=extra["category"], owner=who).first()
        if cat is None:
            raise ValidationError(
                {"category": "Category is not yours."})
        extra["category"] = str(cat.pk)
    data.update(extra)
    if new_cat is not None:
        data["category"] = ""
        data["new_category"] = new_name
    form = RuleDefinitionForm(data=data, instance=obj, user=who)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.owner = who
    if new_cat is not None:
        tmp.category = new_cat
    tmp.full_clean()
    return tmp


def _validate_rule_activation(farm, cmd, actor):
    from portal import models as m
    op = cmd["op"]
    who = _actor_or_owner(farm, actor)
    if op == "create":
        rule_id = cmd["fields"].get("rule")
        rule = m.RuleDefinition.objects.filter(pk=rule_id).first()
        if rule is None:
            raise ValidationError({"rule": "Rule does not exist."})
        if not (rule.owner_id == farm.owner_id or rule.is_public):
            raise ValidationError(
                {"rule": "Rule is not eligible for this farm: only "
                         "farm-author or public rules may install."})
        if m.RuleActivation.objects.filter(
                farm=farm, rule=rule).exists():
            raise ValidationError(
                {"rule": "Rule is already installed on this farm."})
        obj = m.RuleActivation(farm=farm, rule=rule,
                               activated_by=who, is_active=True)
        if "is_active" in cmd["fields"]:
            obj.is_active = bool(cmd["fields"]["is_active"])
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, actor, "Rule activation")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    if "rule" in cmd["fields"] and \
            cmd["fields"]["rule"] != obj.rule_id:
        raise ValidationError(
            {"rule": "Activation rule cannot be swapped; create a new "
                     "activation instead."})
    if "is_active" in cmd["fields"]:
        obj.is_active = bool(cmd["fields"]["is_active"])
    obj.full_clean()
    return obj


def _validate_equipment_property(farm, cmd):
    from portal import models as m
    op = cmd["op"]
    if op == "create":
        eq_id = cmd["fields"].get("equipment")
        prop_id = cmd["fields"].get("property")
        eq = m.Equipment.objects.filter(pk=eq_id, farm=farm).first()
        if eq_id is not None and eq is None:
            raise ValidationError(
                {"equipment": "Cross-farm equipment is not allowed."})
        prop = m.FarmProperty.objects.filter(pk=prop_id, farm=farm).first()
        if prop_id is not None and prop is None:
            raise ValidationError(
                {"property": "Cross-farm property is not allowed."})
        obj = m.EquipmentProperty(equipment=eq, property=prop)
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, None, "Equipment link")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    eq_id = cmd["fields"].get("equipment", obj.equipment_id)
    prop_id = cmd["fields"].get("property", obj.property_id)
    eq = m.Equipment.objects.filter(pk=eq_id, farm=farm).first()
    if eq_id is not None and eq is None:
        raise ValidationError(
            {"equipment": "Cross-farm equipment is not allowed."})
    prop = m.FarmProperty.objects.filter(pk=prop_id, farm=farm).first()
    if prop_id is not None and prop is None:
        raise ValidationError(
            {"property": "Cross-farm property is not allowed."})
    obj.equipment = eq
    obj.property = prop
    obj.full_clean()
    return obj


def _validate_livestock_group(farm, cmd):
    from portal.domain_forms import LivestockGroupEditForm, LivestockGroupForm
    op = cmd["op"]
    if op == "create":
        if "is_active" in cmd["fields"] and \
                cmd["fields"]["is_active"] is False:
            raise ValidationError(
                {"is_active": "Groups are created active; archive via update."})
        if "headcount" not in cmd["fields"]:
            raise ValidationError(
                {"headcount": "Initial headcount is required."})
        data = {k: cmd["fields"].get(k) for k in (
            "name", "species", "breed_label", "headcount",
            "housing_property", "notes")}
        if data["housing_property"] is None:
            data["housing_property"] = ""
        form = LivestockGroupForm(data=data, farm=farm)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.farm = farm
        obj.is_active = True
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, None, "Livestock group")
    if op == "delete":
        from portal.livestock_views import _delete_block_reasons
        reasons = _delete_block_reasons(obj)
        if reasons:
            raise ValidationError(
                "Delete blocked: archive instead (%s)." % "; ".join(
                    str(r) for r in reasons))
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    if "headcount" in cmd["fields"]:
        raise ValidationError(
            {"headcount": "Headcount never edits directly; record an event."})
    data = {"name": obj.name, "species": obj.species,
            "breed_label": obj.breed_label or "",
            "housing_property": str(obj.housing_property_id)
            if obj.housing_property_id else "",
            "notes": obj.notes or ""}
    for key in ("name", "species", "breed_label", "housing_property",
                "notes"):
        if key in cmd["fields"]:
            value = cmd["fields"][key]
            data[key] = "" if value is None and key != "housing_property" \
                else value
    form = LivestockGroupEditForm(data=data, instance=obj, farm=farm)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    if "is_active" in cmd["fields"]:
        tmp.is_active = bool(cmd["fields"]["is_active"])
    tmp.farm = farm
    tmp.full_clean()
    return tmp


def _require_fact_ack(fields):
    if fields.get("fact_ack") is not True:
        raise ValidationError(
            {"fact_ack": "Factual user confirmation (fact_ack=true) "
                         "is required: the server never invents facts."})


def _validate_livestock_event(farm, cmd, actor):
    from portal import models as m
    op = cmd["op"]
    if op == "update":
        raise ValidationError(
            "Livestock history is immutable; reverse the latest event "
            "instead of editing it.")
    if op == "delete":
        from django.db.models import Q as _Q
        from portal import models as _m
        obj = _get_owned(farm, cmd, actor, "Livestock event")
        latest = _m.LivestockEvent.objects.filter(
            _Q(group_id=obj.group_id) | _Q(target_group_id=obj.group_id)
            ).order_by("-event_date", "-id").first()
        if latest is None or latest.pk != obj.pk:
            raise ValidationError(
                "Only the latest event of the group can be reversed.")
        if obj.target_group_id:
            t_latest = _m.LivestockEvent.objects.filter(
                _Q(group_id=obj.target_group_id)
                | _Q(target_group_id=obj.target_group_id)
                ).order_by("-event_date", "-id").first()
            if t_latest is None or t_latest.pk != obj.pk:
                raise ValidationError(
                    "The move cannot be reversed: the target group "
                    "has a newer event.")
        if not obj.group.is_active or (
                obj.target_group_id and not obj.target_group.is_active):
            raise ValidationError(
                "An archived group event cannot be reversed.")
        if _m.WorkItem.objects.filter(
                completed_event_id=obj.pk).exists():
            raise ValidationError(
                "The event is linked to done work and cannot be reversed.")
        return obj
    fields = cmd["fields"]
    _require_fact_ack(fields)
    statement = str(fields.get("fact_statement") or "").strip()
    if not statement:
        raise ValidationError(
            {"fact_statement": "A factual user declaration is required; "
                               "the adapter never invents kind or counts."})
    group_id = fields.get("group")
    group = m.LivestockGroup.objects.filter(
        pk=group_id, farm=farm).first()
    if group is None:
        raise ValidationError({"group": "Group is not on this farm."})
    target = None
    if fields.get("target_group") is not None:
        target = m.LivestockGroup.objects.filter(
            pk=fields["target_group"], farm=farm).first()
        if target is None:
            raise ValidationError(
                {"target_group": "Target group is not on this farm."})
    prop = None
    if fields.get("property") is not None:
        prop = m.FarmProperty.objects.filter(
            pk=fields["property"], farm=farm).first()
        if prop is None:
            raise ValidationError(
                {"property": "Cross-farm property is not allowed."})
    notes = str(fields.get("notes") or "").strip() or statement
    probe = m.LivestockEvent(
        farm=farm, group=group, target_group=target,
        event_date=fields.get("event_date"), kind=fields.get("kind"),
        headcount_delta=fields.get("headcount_delta") or 0,
        property=prop, notes=notes)
    probe.full_clean()
    return probe


def _validate_livestock_work(farm, cmd):
    from portal import models as m
    from portal.domain_forms import LivestockWorkItemForm
    op = cmd["op"]
    if op == "create":
        group_id = cmd["fields"].get("livestock_group")
        group = m.LivestockGroup.objects.filter(
            pk=group_id, farm=farm).first()
        if group is None:
            raise ValidationError(
                {"livestock_group": "Group is not on this farm."})
        if not group.is_active:
            raise ValidationError(
                {"livestock_group": "Archived group takes no new work."})
        data = {k: cmd["fields"].get(k) for k in (
            "livestock_kind", "title", "start_date", "end_date",
            "estimated_hours", "property", "notes")}
        if data["property"] is None:
            data["property"] = ""
        form = LivestockWorkItemForm(data=data, farm=farm, group=group)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.farm = farm
        obj.livestock_group = group
        obj.crop_cycle = None
        obj.capability = "other"
        obj.equipment = None
        obj.missing = False
        obj.recommendation = (form.cleaned_data.get("notes") or "")[:240]
        obj.full_clean()
        return obj
    qs = scoped_qs("livestock_work", farm)
    try:
        obj = qs.get(pk=cmd["id"])
    except Exception:
        raise ValidationError(
            "Livestock work #%s is not available." % cmd["id"])
    if not obj.livestock_group.is_active:
        raise ValidationError("Archived group work cannot change.")
    if op == "delete":
        if obj.status == "done" or obj.completed_event_id:
            raise ValidationError(
                "Done work is history; it cannot be deleted.")
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    if obj.status == "done" or obj.completed_event_id:
        raise ValidationError("Done work is history and cannot change.")
    data = {"livestock_kind": obj.livestock_kind or "other",
            "title": obj.title,
            "start_date": obj.start_date.isoformat()
            if obj.start_date else "",
            "end_date": obj.end_date.isoformat() if obj.end_date else "",
            "estimated_hours": str(obj.estimated_hours),
            "property": str(obj.property_id) if obj.property_id else "",
            "notes": obj.recommendation or ""}
    for key, value in cmd["fields"].items():
        if key == "livestock_group":
            continue
        data[key] = "" if value is None else value
    if "livestock_group" in cmd["fields"] and \
            cmd["fields"]["livestock_group"] != obj.livestock_group_id:
        raise ValidationError(
            {"livestock_group": "Work stays with its group; create new work."})
    form = LivestockWorkItemForm(
        data=data, instance=obj, farm=farm, group=obj.livestock_group)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.farm = farm
    tmp.crop_cycle = None
    tmp.capability = "other"
    tmp.equipment = None
    tmp.missing = False
    tmp.recommendation = (form.cleaned_data.get("notes") or "")[:240]
    tmp.full_clean()
    return tmp


def _validate_farming_event(farm, cmd, actor):
    from portal.domain_forms import FarmingEventForm
    op = cmd["op"]
    who = _actor_or_owner(farm, actor)
    if op == "create":
        _require_fact_ack(cmd["fields"])
        data = {k: cmd["fields"].get(k) for k in (
            "property", "crop_cycle", "crop_name_snapshot", "event_date",
            "event_type", "description", "quantity", "unit", "materials",
            "treated_area_hectares", "notes")}
        form = FarmingEventForm(data=data, farm=farm)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.farm = farm
        obj.created_by = who
        obj.updated_by = who
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, actor, "Farming event")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    _require_fact_ack(cmd["fields"])
    data = {"property": str(obj.property_id),
            "crop_cycle": str(obj.crop_cycle_id)
            if obj.crop_cycle_id else "",
            "crop_name_snapshot": obj.crop_name_snapshot or "",
            "event_date": obj.event_date.isoformat()
            if obj.event_date else "",
            "event_type": obj.event_type,
            "description": obj.description,
            "quantity": str(obj.quantity) if obj.quantity is not None
            else "",
            "unit": obj.unit or "", "materials": obj.materials or "",
            "treated_area_hectares": str(obj.treated_area_hectares)
            if obj.treated_area_hectares is not None else "",
            "notes": obj.notes or ""}
    for key, value in cmd["fields"].items():
        if key == "fact_ack":
            continue
        data[key] = "" if value is None else value
    form = FarmingEventForm(data=data, instance=obj, farm=farm)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.farm = farm
    tmp.updated_by = who
    if tmp.created_by_id is None:
        tmp.created_by = who
    tmp.full_clean()
    return tmp


def _validate_farm_document(farm, cmd, actor):
    from portal import models as m
    from portal.farm_knowledge import is_eligible, require_manager
    from portal.permissions import role_for
    who = _actor_or_owner(farm, actor)
    if role_for(who, farm) not in ("owner", "manager"):
        raise PermissionDenied("Role may not manage knowledge installs.")
    op = cmd["op"]
    if op == "create":
        doc = m.Document.objects.filter(
            pk=cmd["fields"].get("document")).first()
        if doc is None:
            raise ValidationError({"document": "Document is not visible."})
        if not is_eligible(doc, farm):
            raise ValidationError(
                {"document": "Document is not eligible for this farm: "
                             "only the farm-author or public documents "
                             "may install."})
        if m.FarmDocument.objects.filter(
                farm=farm, document=doc).exists():
            raise ValidationError(
                {"document": "Document is already installed on this farm."})
        obj = m.FarmDocument(farm=farm, document=doc, added_by=who,
                             is_active=True)
        if "is_active" in cmd["fields"]:
            obj.is_active = bool(cmd["fields"]["is_active"])
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, actor, "Farm document")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    if "document" in cmd["fields"] and \
            cmd["fields"]["document"] != obj.document_id:
        raise ValidationError(
            {"document": "Install document cannot be swapped; create anew."})
    if "is_active" in cmd["fields"]:
        obj.is_active = bool(cmd["fields"]["is_active"])
    obj.full_clean()
    return obj


def _validate_news_source(farm, cmd, actor):
    from portal.domain_forms import RuleCategoryForm  # noqa: F401 (keep import style)
    from portal.news_forms import NewsSourceForm
    who = _actor_or_owner(farm, actor)
    op = cmd["op"]
    if op == "create":
        form = NewsSourceForm(data=dict(cmd["fields"]))
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.owner = who
        obj.full_clean()
        return obj
    obj = _get_owned(farm, cmd, actor, "News source")
    if obj.owner_id != who.pk:
        raise ValidationError("News source is not yours.")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    data = {"name": obj.name, "url": obj.url,
            "notes": obj.notes or "", "is_active": obj.is_active}
    data.update(cmd["fields"])
    form = NewsSourceForm(data=data, instance=obj)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.owner = who
    tmp.full_clean()
    return tmp


def _validate_membership(farm, cmd, actor):
    from portal import models as m
    op = cmd["op"]
    if op == "create":
        raise ValidationError(
            "Membership create is not offered: invite the exact user "
            "email, then the user accepts the invitation.")
    obj = _get_owned(farm, cmd, actor, "Membership")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    fields = cmd["fields"]
    if "role" in fields:
        if fields["role"] not in ("manager", "worker", "viewer"):
            raise ValidationError({"role": "Unknown role."})
        obj.role = fields["role"]
    if "is_active" in fields:
        obj.is_active = bool(fields["is_active"])
    if obj.user_id == farm.owner_id:
        raise ValidationError("The farm owner has no membership row.")
    obj.full_clean()
    return obj


def _validate_invitation(farm, cmd, actor):
    from django.utils import timezone as _tz
    from portal import models as m
    from portal.forms import InviteForm
    op = cmd["op"]
    if op == "create":
        form = InviteForm(data={k: cmd["fields"].get(k) for k in (
            "email", "role")})
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        email = form.cleaned_data["email"].strip().lower()
        role = form.cleaned_data["role"]
        now = _tz.now()
        if m.Invitation.objects.filter(
                farm=farm, email=email, used_at__isnull=True,
                revoked_at__isnull=True, expires_at__gt=now).exists():
            raise ValidationError(
                {"email": "A pending invitation already exists."})
        from django.contrib.auth import get_user_model as _gum
        existing = _gum().objects.filter(email=email).first()
        if existing is not None and m.Membership.objects.filter(
                farm=farm, user=existing, is_active=True).exists():
            raise ValidationError(
                {"email": "This user is already an active member."})
        obj = m.Invitation(farm=farm, email=email, role=role,
                           token_hash="pending-server-token",
                           created_by=_actor_or_owner(farm, actor),
                           expires_at=now + _tz.timedelta(hours=72))
        obj.full_clean(exclude=["token_hash"])
        return obj
    obj = _get_owned(farm, cmd, actor, "Invitation")
    if op == "delete":
        raise ValidationError(
            "Invitation delete is not offered: revoke instead so the "
            "audit trail is retained; rows never cascade.")
    if cmd["fields"].get("revoked") is not True and \
            set(cmd["fields"]) != {"revoked"}:
        pass
    if obj.used_at is not None:
        raise ValidationError("A used invitation cannot be revoked.")
    if obj.revoked_at is not None:
        raise ValidationError("The invitation is already revoked.")
    return obj


def _validate_document_meta(farm, cmd, actor):
    from portal.document_forms import DocumentForm
    who = _actor_or_owner(farm, actor)
    op = cmd["op"]
    if op == "create":
        raise ValidationError(
            "Document create is not offered through changesets: upload "
            "only via the independent manual flow with a server-validated "
            "upload; arbitrary paths are never accepted.")
    obj = _get_owned(farm, cmd, actor, "Document")
    if obj.owner_id != who.pk:
        raise ValidationError("Document is not yours.")
    if op == "delete":
        _enforce_adapter_delete(cmd["target"], obj)
        return obj
    data = {"name": obj.name, "description": obj.description or "",
            "is_public": obj.is_public}
    data.update(cmd["fields"])
    form = DocumentForm(data=data, instance=obj, require_upload=False)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.instance
    tmp.owner = who
    tmp.full_clean()
    return tmp


def _enforce_adapter_delete(target, obj):
    # New targets cascade only to expected rows; unknown cascades and
    # protected history block rather than delete silently. Uses the
    # shared Collector enumeration from the registry (deferred import
    # avoids a module cycle).
    from .registry import delete_impact
    from django.db.models import ProtectedError
    impact = delete_impact(obj)
    bad = [e for e in impact.get("cascades", [])
           if e["model"].split(".")[0] == "portal"
           and e["model"] not in _EXPECTED_CASCADES.get(target, ())]
    if bad:
        raise ValidationError(
            f"Delete blocked: unexpected cascade "
            f"{bad[0]['model']} is linked.")
    return impact


_EXPECTED_CASCADES = {
    "farm": (),
    "extra_field": (),
    "location": (),
    "property": ("portal.EquipmentProperty",),
    "crop_cycle": (),
    "observation": (),
    "external_contact": (),
    "rule_category": ("portal.RuleDefinition",),
    "rule_definition": ("portal.RuleActivation",),
    "rule_activation": (),
    "equipment_property": (),
    "livestock_group": (),
    "livestock_event": (),
    "livestock_work": (),
    "farming_event": (),
    "farm_document": (),
    "news_source": (),
    "membership": (),
    "invitation": (),
    "document": ("portal.FarmDocument",),
}


# ---------------------------------------------------------------------------
# Execution (writes; caller revalidates consent/role before calling)
# ---------------------------------------------------------------------------

def execute(farm, cmd, actor=None):
    """Run one validated new-target command; returns (pk, after, extra)."""
    from .registry import delete_impact
    target, op = cmd["target"], cmd["op"]
    if target == "observation":
        obj = validate(farm, cmd, actor)
        pk = obj.pk
        obj.save()
        obj.refresh_from_db()
        return pk, dump(target, obj), {}
    if target == "livestock_event" and op == "create":
        return _exec_livestock_event_create(farm, cmd, actor)
    if target == "livestock_event" and op == "delete":
        obj = validate(farm, cmd, actor)
        pk = obj.pk
        impact = delete_impact(obj)
        obj.reverse_delete()
        return pk, None, {"impact": impact, "related_writes": []}
    if target == "invitation" and op == "create":
        return _exec_invitation_create(farm, cmd, actor)
    if target == "invitation" and op == "update":
        return _exec_invitation_revoke(farm, cmd, actor)
    if op == "create":
        return _exec_create(farm, cmd, actor)
    obj = validate(farm, cmd, actor)
    pk = obj.pk
    if op == "delete":
        impact = delete_impact(obj)
        _enforce_adapter_delete(target, obj)
        if target == "rule_category":
            from portal import models as m
            linked = sorted(m.RuleDefinition.objects.filter(
                category=obj).values_list("pk", flat=True))
            obj.delete()
            related = [{"model": "portal.RuleDefinition", "id": lid,
                        "field": "category", "before": pk,
                        "after": None} for lid in linked]
            return pk, None, {"impact": impact,
                              "related_writes": related}
        obj.delete()
        return pk, None, {"impact": impact, "related_writes": []}
    obj.save()
    obj.refresh_from_db()
    return obj.pk, dump(target, obj), {}


def _exec_create(farm, cmd, actor):
    target = cmd["target"]
    obj = validate(farm, cmd, actor)
    try:
        obj.save()
    except Exception as exc:
        from django.db import IntegrityError
        if isinstance(exc, IntegrityError):
            raise ValidationError(
                f"{target} already exists (duplicate unique value).")
        raise
    obj.refresh_from_db()
    if target == "rule_activation":
        who = _actor_or_owner(farm, actor)
        if obj.activated_by_id is None:
            obj.activated_by = who
            obj.save(update_fields=["activated_by"])
            obj.refresh_from_db()
    return obj.pk, dump(target, obj), {}


def _exec_livestock_event_create(farm, cmd, actor):
    from portal import models as m
    fields = cmd["fields"]
    group = m.LivestockGroup.objects.get(
        pk=fields["group"], farm=farm)
    target = None
    if fields.get("target_group") is not None:
        target = m.LivestockGroup.objects.get(
            pk=fields["target_group"], farm=farm)
    prop = None
    if fields.get("property") is not None:
        prop = m.FarmProperty.objects.get(
            pk=fields["property"], farm=farm)
    notes = str(fields.get("notes") or "").strip() or \
        str(fields.get("fact_statement") or "").strip()
    who = _actor_or_owner(farm, actor)
    try:
        ev = m.LivestockEvent.record(
            farm=farm, group=group,
            event_date=fields.get("event_date"), kind=fields.get("kind"),
            headcount_delta=fields.get("headcount_delta") or 0,
            target_group=target, property=prop, notes=notes,
            create_journal=bool(fields.get("create_journal")),
            actor=who)
    except ValidationError:
        raise
    except Exception as exc:
        from django.db import IntegrityError
        if isinstance(exc, IntegrityError):
            raise ValidationError("Duplicate livestock event.")
        raise ValidationError(str(exc))
    return ev.pk, dump("livestock_event", ev), {}


def _exec_invitation_create(farm, cmd, actor):
    import hashlib as _hashlib
    import secrets as _secrets
    from datetime import timedelta as _td
    from django.utils import timezone as _tz
    from portal import models as m
    who = _actor_or_owner(farm, actor)
    now = _tz.now()
    # Reuse the manual flow values: exact email, typed role, 72h expiry.
    # The raw token is never stored: only its SHA-256 hash persists and
    # no email is sent from the changeset path.
    raw = _secrets.token_urlsafe(32)
    obj = m.Invitation(
        farm=farm, email=cmd["fields"]["email"].strip().lower(),
        role=cmd["fields"]["role"],
        token_hash=_hashlib.sha256(raw.encode()).hexdigest(),
        created_by=who, expires_at=now + _td(hours=72))
    obj.full_clean()
    try:
        obj.save()
    except Exception as exc:
        from django.db import IntegrityError
        if isinstance(exc, IntegrityError):
            raise ValidationError(
                "invitation already exists (duplicate value).")
        raise
    after = dump("invitation", obj)
    after["token_hash"] = obj.token_hash
    return obj.pk, after, {}


def _exec_invitation_revoke(farm, cmd, actor):
    from django.utils import timezone as _tz
    obj = validate(farm, cmd, actor)
    obj.revoked_at = _tz.now()
    obj.save(update_fields=["revoked_at"])
    obj.refresh_from_db()
    return obj.pk, dump("invitation", obj), {}


# ---------------------------------------------------------------------------
# Snapshots (explicit, sorted, scoped)
# ---------------------------------------------------------------------------

def dump(target, obj):
    """Small explicit snapshot dict for one row."""
    if target == "farm":
        out = {"id": obj.pk}
        for key in sorted(_FARM_FIELDS):
            out[key] = getattr(obj, key)
        return out
    if target == "extra_field":
        return {"id": obj.pk, "key": obj.key, "value": obj.value,
                "position": obj.position}
    if target == "location":
        return {"id": obj.pk, "name": obj.name,
                "latitude": str(obj.latitude),
                "longitude": str(obj.longitude),
                "timezone": obj.timezone}
    if target == "property":
        return {"id": obj.pk, "location": obj.location_id,
                "name": obj.name, "kind": obj.kind,
                "area_hectares": str(obj.area_hectares),
                "notes": obj.notes or ""}
    if target == "crop_cycle":
        return {"id": obj.pk, "property": obj.property_id,
                "crop_name": obj.crop_name, "state": obj.state,
                "sow_date": obj.sow_date.isoformat()
                if obj.sow_date else None,
                "observations": obj.observations or ""}
    if target == "observation":
        return {"id": obj.pk, "observations": obj.observations or ""}
    if target == "external_contact":
        return {"id": obj.pk, "name": obj.name, "phone": obj.phone,
                "email": obj.email, "services": obj.services,
                "notes": obj.notes or "",
                "availability": obj.availability or ""}
    if target == "rule_category":
        return {"id": obj.pk, "name": obj.name}
    if target == "rule_definition":
        return {"id": obj.pk, "name": obj.name,
                "category": obj.category_id, "is_public": obj.is_public}
    if target == "rule_activation":
        return {"id": obj.pk, "rule": obj.rule_id,
                "is_active": bool(obj.is_active),
                "activated_by": obj.activated_by_id}
    if target == "equipment_property":
        return {"id": obj.pk, "equipment": obj.equipment_id,
                "property": obj.property_id}
    if target == "livestock_group":
        return {"id": obj.pk, "name": obj.name, "species": obj.species,
                "breed_label": obj.breed_label or "",
                "headcount": int(obj.headcount),
                "housing_property": obj.housing_property_id,
                "notes": obj.notes or "",
                "is_active": bool(obj.is_active)}
    if target == "livestock_event":
        return {"id": obj.pk, "group": obj.group_id,
                "event_date": obj.event_date.isoformat()
                if obj.event_date else None,
                "kind": obj.kind,
                "headcount_delta": int(obj.headcount_delta or 0),
                "target_group": obj.target_group_id,
                "property": obj.property_id,
                "notes": obj.notes or ""}
    if target == "livestock_work":
        return {"id": obj.pk, "livestock_group": obj.livestock_group_id,
                "livestock_kind": obj.livestock_kind or "",
                "title": obj.title,
                "start_date": obj.start_date.isoformat()
                if obj.start_date else None,
                "end_date": obj.end_date.isoformat()
                if obj.end_date else None,
                "estimated_hours": str(obj.estimated_hours),
                "property": obj.property_id,
                "status": obj.status}
    if target == "farming_event":
        return {"id": obj.pk, "property": obj.property_id,
                "crop_cycle": obj.crop_cycle_id,
                "crop_name_snapshot": obj.crop_name_snapshot or "",
                "event_date": obj.event_date.isoformat()
                if obj.event_date else None,
                "event_type": obj.event_type,
                "description": obj.description,
                "quantity": str(obj.quantity)
                if obj.quantity is not None else None,
                "unit": obj.unit or "", "materials": obj.materials or "",
                "treated_area_hectares": str(obj.treated_area_hectares)
                if obj.treated_area_hectares is not None else None,
                "notes": obj.notes or ""}
    if target == "farm_document":
        return {"id": obj.pk, "document": obj.document_id,
                "is_active": bool(obj.is_active),
                "added_by": obj.added_by_id}
    if target == "news_source":
        return {"id": obj.pk, "name": obj.name, "url": obj.url,
                "notes": obj.notes or "",
                "is_active": bool(obj.is_active)}
    if target == "membership":
        return {"id": obj.pk, "user": obj.user_id, "role": obj.role,
                "is_active": bool(obj.is_active)}
    if target == "invitation":
        return {"id": obj.pk, "email": obj.email, "role": obj.role,
                "used_at": obj.used_at.isoformat() if obj.used_at else None,
                "revoked_at": obj.revoked_at.isoformat()
                if obj.revoked_at else None}
    if target == "document":
        return {"id": obj.pk, "name": obj.name,
                "description": obj.description or "",
                "is_public": bool(obj.is_public)}
    raise ValueError(f"Unknown adapter target: {target!r}")


def snapshot_rows(target, farm, actor=None):
    """All in-scope rows for one target, explicitly sorted by pk."""
    qs = scoped_qs(target, farm, actor)
    return [dump(target, o) for o in qs.order_by("pk")]

