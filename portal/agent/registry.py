"""Typed command registry for the approval executor (slices 1-2).

Targets: action, equipment, work_item plus 20 typed adapters. The
canonical spec name ``cycle`` is accepted as an alias of ``crop_cycle``.
Ops: create, update, delete. Validation uses the real forms and model
full_clean with farm-scoped querysets; permissions mirror the manual
views; foreign keys are scoped to the same farm; delete impact is
checked with Django's Collector before any write. FAMILY_OPS below
names all 28 spec families with per-op support and the exact reason
where an op stays visibly blocked (including protected technical rows).
"""
import json
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models.deletion import Collector

from . import adapters as _adapters

TARGETS = ("action", "equipment", "work_item") + _adapters.NEW_TARGETS
OPS = ("create", "update", "delete")


def canonical_target(target):
    """Canonical spec alias: ``cycle`` means ``crop_cycle``."""
    return "crop_cycle" if target == "cycle" else target


# Static family map for the 28 spec families: True means the op is
# implemented through changesets, a string names the actual block reason.
FAMILY_OPS = {
    "action": {"read": True, "create": True, "update": True, "delete": True},
    "equipment": {"read": True, "create": True, "update": True, "delete": True},
    "work_item": {"read": True, "create": True, "update": True, "delete": True},
    "farm": {"read": True, "create": "Farm create is not offered through changesets: farm rows are created only with the user account.",
             "update": True, "delete": "Farm delete is not offered through changesets; use account deletion."},
    "extra_field": {"read": True, "create": True, "update": True, "delete": True},
    "location": {"read": True, "create": True, "update": True, "delete": True},
    "property": {"read": True, "create": True, "update": True, "delete": True},
    "crop_cycle": {"read": True, "create": True, "update": True, "delete": True},
    "observation": {"read": True, "create": "Observation create is not offered; update the cycle observations instead.",
                    "update": True, "delete": "Observation delete is not offered; update the cycle with an empty observation to clear it."},
    "external_contact": {"read": True, "create": True, "update": True, "delete": True},
    "rule_category": {"read": True, "create": True, "update": True, "delete": True},
    "rule_definition": {"read": True, "create": True, "update": True, "delete": True},
    "rule_activation": {"read": True, "create": True, "update": True, "delete": True},
    "equipment_property": {"read": True, "create": True, "update": True, "delete": True},
    "livestock_group": {"read": True, "create": True, "update": True, "delete": True},
    "livestock_event": {"read": True, "create": True,
                        "update": "Livestock history is immutable; reverse the latest event instead of editing it.",
                        "delete": True},
    "livestock_work": {"read": True, "create": True, "update": True, "delete": True},
    "farming_event": {"read": True, "create": True, "update": True, "delete": True},
    "farm_document": {"read": True, "create": True, "update": True, "delete": True},
    "news_source": {"read": True, "create": True, "update": True, "delete": True},
    "membership": {"read": True, "create": "Membership create is not offered: invite the exact user email, then the user accepts the invitation.",
                   "update": True, "delete": True},
    "invitation": {"read": True, "create": True, "update": True,
                   "delete": "Invitation delete is not offered: revoke instead so the audit trail is retained."},
    "document": {"read": True, "create": "Document create is not offered through changesets: upload only via the independent manual flow with a server-validated upload.",
                 "update": True, "delete": True},
    "sowing_review": {"read": "Sowing review is a business decision step, not a stored row write.",
                      "create": "Sowing review is a business decision step, not a stored row write.",
                      "update": "Sowing review is a business decision step, not a stored row write.",
                      "delete": "Sowing review is a business decision step, not a stored row write."},
    "plan_revision": {"read": "Plan revisions are business plan states applied via farming events, not a direct row write.",
                      "create": "Plan revisions are business plan states applied via farming events, not a direct row write.",
                      "update": "Plan revisions are business plan states applied via farming events, not a direct row write.",
                      "delete": "Plan revisions are business plan states applied via farming events, not a direct row write."},
    "conversation": {"read": "Conversation is internal agent infrastructure, not farm domain state.",
                     "create": "Conversation is internal agent infrastructure, not farm domain state.",
                     "update": "Conversation is internal agent infrastructure, not farm domain state.",
                     "delete": "Conversation is internal agent infrastructure, not farm domain state."},
    "message": {"read": "Message is internal agent infrastructure, not farm domain state.",
                "create": "Message is internal agent infrastructure, not farm domain state.",
                "update": "Message is internal agent infrastructure, not farm domain state.",
                "delete": "Message is internal agent infrastructure, not farm domain state."},
    "agent_run": {"read": "Agent runs are internal execution records managed by the run API, not changeset targets.",
                 "create": "Agent runs are internal execution records managed by the run API, not changeset targets.",
                 "update": "Agent runs are internal execution records managed by the run API, not changeset targets.",
                 "delete": "Agent runs are internal execution records managed by the run API, not changeset targets."},
}

_ACTION_FIELDS = frozenset({"text", "status"})
_EQUIPMENT_FIELDS = frozenset({
    "name", "capabilities", "available", "hours_per_day", "properties"})
_WORK_ITEM_FIELDS = frozenset({
    "title", "capability", "start_date", "end_date", "estimated_hours",
    "property", "crop_cycle", "equipment", "status"})

_COMMAND_KEYS = frozenset({"target", "op", "id", "ref", "fields"})


class CommandError(ValueError):
    pass


def _role_of(actor, farm):
    from portal.permissions import role_for
    return role_for(actor, farm)


def check_permission(actor, farm, target, op=None):
    from django.core.exceptions import PermissionDenied
    target = canonical_target(target)
    if target in _adapters.NEW_TARGETS:
        # Adapter descriptors carry their own per-op role allowlist;
        # op is unknown at prepare-apply recheck time, so fall back to
        # the union gate there and enforce the exact op in validate.
        if op is None:
            allowed = set()
            for ops in _adapters.ROLES[target].values():
                allowed.update(ops)
            role = _role_of(actor, farm)
            scope = _adapters.SCOPE[target]
            if scope == "owner":
                _adapters._active_or_denied(actor)
                if role is None and \
                        getattr(farm, "owner_id", None) != actor.pk:
                    raise PermissionDenied(
                        f"Role None may not write {target}.")
                return role
            if role not in allowed:
                raise PermissionDenied(
                    f"Role {role!r} may not write {target}.")
            return role
        return _adapters.check_permission(actor, farm, target, op)
    role = _role_of(actor, farm)
    if target == "action":
        allowed = ("owner", "manager", "worker")
    elif target in ("equipment", "work_item"):
        allowed = ("owner", "manager")
    else:
        raise CommandError(f"Unknown target: {target!r}")
    if role not in allowed:
        raise PermissionDenied(
            f"Role {role!r} may not write {target}.")
    return role


def parse_command(raw):
    """Validate command shape; reject unknown targets, ops and keys."""
    if not isinstance(raw, dict):
        raise CommandError("Command must be an object.")
    unknown = set(raw) - _COMMAND_KEYS
    if unknown:
        raise CommandError(
            f"Unknown command keys rejected: {sorted(unknown)}")
    target = canonical_target(raw.get("target"))
    op = raw.get("op")
    if target not in TARGETS:
        raise CommandError(f"Unknown target: {target!r}")
    if op not in OPS:
        raise CommandError(f"Unknown op: {op!r}")
    fields = raw.get("fields", {})
    if op == "delete" and fields not in (None, {}, []):
        raise CommandError("Delete takes no fields.")
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        raise CommandError("Fields must be an object.")
    if target in _adapters.NEW_TARGETS:
        allow = _adapters.FIELDS[target]
    else:
        allow = {"action": _ACTION_FIELDS,
                 "equipment": _EQUIPMENT_FIELDS,
                 "work_item": _WORK_ITEM_FIELDS}[target]
    unknown_fields = set(fields) - set(allow)
    if unknown_fields:
        raise CommandError(
            f"Unknown {target} fields rejected: {sorted(unknown_fields)}")
    ref = raw.get("ref", "")
    obj_id = raw.get("id")
    if op == "create" and obj_id is not None:
        raise CommandError("Create takes no id.")
    if op in ("update", "delete") and obj_id is None:
        raise CommandError(f"{op} requires an id.")
    return {"target": target, "op": op, "id": obj_id, "ref": ref or "",
            "fields": dict(fields)}


def _scoped_qs(target, farm, actor=None):
    target = canonical_target(target)
    if target in _adapters.NEW_TARGETS:
        return _adapters.scoped_qs(target, farm, actor)
    from portal.models import Equipment, FarmAction, WorkItem
    if target == "action":
        return FarmAction.objects.filter(farm=farm)
    if target == "equipment":
        return Equipment.objects.filter(farm=farm)
    return WorkItem.objects.filter(farm=farm)


def _check_scoped_fk(farm, obj, field_name, farm_attr="farm_id"):
    ref = getattr(obj, field_name, None)
    if ref is None:
        return
    fid = getattr(ref, farm_attr, None)
    if fid is None:
        try:
            fid = ref.farm_id
        except Exception:
            fid = None
    if fid is not None and fid != farm.pk:
        raise ValidationError(
            {field_name: "Cross-farm reference is not allowed."})


def validate_one(farm, cmd, actor=None):
    """Dry-run validation of one parsed command; writes nothing."""
    if cmd["target"] in _adapters.NEW_TARGETS:
        return _adapters.validate(farm, cmd, actor)
    from portal.domain_forms import EquipmentForm
    from portal.forms import ActionForm
    target, op = cmd["target"], cmd["op"]
    fields, obj_id = cmd["fields"], cmd["id"]
    if op == "create":
        if target == "action":
            form = ActionForm(data={"text": fields.get("text", "")})
            if not form.is_valid():
                raise ValidationError(form.errors.as_json())
            obj = form.instance
            obj.farm = farm
            status = fields.get("status", "open")
            if status not in ("open", "done"):
                raise ValidationError({"status": "Unknown status."})
            if status == "done":
                raise ValidationError(
                    {"status": "Changesets only create open actions."})
            obj.status = "open"
            obj.full_clean(exclude=["farm"])
            return obj
        if target == "equipment":
            data = _equipment_form_data(fields)
            form = EquipmentForm(data=data, farm=farm)
            if not form.is_valid():
                raise ValidationError(form.errors.as_json())
            obj = form.instance
            obj.farm = farm
            obj.full_clean(exclude=["farm"])
            _check_props_farm(farm, form)
            return obj
        # work_item create: crop work only, livestock groups out of scope.
        obj = _build_work_item(farm, fields, instance=None)
        obj.full_clean()
        _validate_work_item_guards(obj, True)
        return obj
    qs = _scoped_qs(target, farm)
    try:
        obj = qs.get(pk=obj_id)
    except Exception:
        raise ValidationError(f"{target} #{obj_id} is not on this farm.")
    if op == "delete":
        if target == "work_item":
            _validate_work_item_guards(obj, False)
        impact = delete_impact(obj)
        _enforce_delete_policy(target, obj, impact)
        return obj
    # update
    if target == "action":
        data = {"text": fields.get("text", obj.text)}
        form = ActionForm(data=data, instance=obj)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        if "status" in fields:
            if fields["status"] not in ("open", "done"):
                raise ValidationError({"status": "Unknown status."})
        tmp = form.instance
        tmp.full_clean(exclude=["farm"])
        return tmp
    if target == "equipment":
        data = _equipment_form_data(fields, base=obj)
        form = EquipmentForm(data=data, instance=obj, farm=farm)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        _check_props_farm(farm, form)
        return form.instance
    obj = _build_work_item(farm, fields, instance=obj)
    obj.full_clean()
    _validate_work_item_guards(obj, False)
    return obj


def _validate_work_item_guards(obj, is_new):
    return _validate_work_item_Guards(obj, is_new)


def _validate_work_item_Guards(obj, is_new):
    if not is_new:
        locked = type(obj).objects.filter(pk=obj.pk).first()
        if locked is not None:
            if getattr(locked, "status", "") == "done" or \
                    getattr(locked, "completed_event_id", None):
                raise ValidationError(
                    "Completed work is historical and cannot change.")
    if is_new and getattr(obj, "status", "open") != "open":
        raise ValidationError(
            {"status": "Changesets only create open work."})


def _equipment_form_data(fields, base=None):
    # Explicit defaults for creates mirror the model layer; updates
    # start from the stored row so missing keys keep their values.
    data = {"available": True, "hours_per_day": "8",
            "capabilities": [], "properties": []}
    if base is not None:
        data = {"name": base.name,
                "available": base.available,
                "hours_per_day": str(base.hours_per_day),
                "capabilities": list(base.capabilities or []),
                "properties": [str(p) for p in
                               base.properties.values_list("pk", flat=True)]}
    for key in ("name", "available", "hours_per_day"):
        if key in fields:
            data[key] = fields[key]
    if "capabilities" in fields:
        data["capabilities"] = fields["capabilities"]
    if "properties" in fields:
        data["properties"] = [str(p) for p in fields["properties"]]
    return data


def _check_props_farm(farm, form):
    props = list(form.cleaned_data.get("properties") or [])
    for prop in props:
        if prop.farm_id != farm.pk:
            raise ValidationError(
                "Cross-farm equipment link is not allowed.")


def _build_work_item(farm, fields, instance=None):
    from portal.models import CropCycle, Equipment, FarmProperty, WorkItem
    obj = instance or WorkItem(farm=farm)
    obj.farm = farm
    if instance is None and obj.status is None:
        obj.status = "open"
    for key in ("title", "capability", "start_date", "end_date",
                "estimated_hours", "status"):
        if key in fields:
            setattr(obj, key, fields[key])
    if "property" in fields:
        pid = fields["property"]
        obj.property = None if pid is None else \
            FarmProperty.objects.filter(pk=pid, farm=farm).first()
        if pid is not None and obj.property is None:
            raise ValidationError(
                {"property": "Cross-farm property is not allowed."})
    if "crop_cycle" in fields:
        cid = fields["crop_cycle"]
        obj.crop_cycle = None if cid is None else \
            CropCycle.objects.filter(pk=cid, farm=farm).first()
        if cid is not None and obj.crop_cycle is None:
            raise ValidationError(
                {"crop_cycle": "Cross-farm cycle is not allowed."})
    if "equipment" in fields:
        eid = fields["equipment"]
        obj.equipment = None if eid is None else \
            Equipment.objects.filter(pk=eid, farm=farm).first()
        if eid is not None and obj.equipment is None:
            raise ValidationError(
                {"equipment": "Cross-farm equipment is not allowed."})
    if instance is None:
        # MVP scope: crop work items only; livestock work keeps its
        # dedicated forms and done-flow.
        from portal.models import WorkItem as _WI
        obj.livestock_group = None
        if not getattr(obj, "status", None):
            obj.status = "open"
    return obj


def delete_impact(obj):
    """Collect related rows without deleting; classify the fallout."""
    from django.db import router
    using = router.db_for_write(type(obj))
    collector = Collector(using=using)
    collector.collect([obj])
    out = {"cascades": [], "protected": [], "set_null": []}
    for model, instances in collector.data.items():
        label = f"{model._meta.app_label}.{model.__name__}"
        count = len(instances)
        if model is type(obj):
            continue
        out["cascades"].append({"model": label, "count": count})
    for (field, _value), instances in collector.field_updates.items():
        label = (f"{field.model._meta.app_label}."
                 f"{field.model.__name__}.{field.name}")
        out["set_null"].append({"model": label, "count": len(instances)})
    return out


def _enforce_delete_policy(target, obj, impact):
    from django.db.models import ProtectedError, Q
    # Any PROTECTed history row blocks the delete (cross-farm rows are
    # already excluded by farm-scoped lookup above).
    for entry in impact.get("cascades", []):
        name = entry["model"]
        if name in ("portal.FarmingEvent", "portal.LivestockEvent",
                    "portal.WorkItem", "portal.CropCycle",
                    "portal.FarmProperty", "portal.Document",
                    "portal.ChatProposal", "portal.AdviceStep"):
            raise ValidationError(
                f"Delete blocked: protected history {name} is linked.")
        if target == "action" and entry["model"] not in (
                "portal.AuditEvent",):
            # Actions cascade to nothing expected; any unknown cascade
            # from another entity blocks rather than deletes silently.
            if entry["model"].startswith("portal."):
                raise ValidationError(
                    f"Delete blocked: unexpected cascade {name}.")
    if target == "work_item":
        # Equipment links SET_NULL and stay visible in the receipt;
        # done history is already blocked by the guards above.
        pass
    if target == "equipment":
        # Rewriting completed work history via SET_NULL is blocked:
        # done work items keep their equipment reference.
        from portal.models import WorkItem
        done_refs = WorkItem.objects.filter(
            farm=obj.farm, equipment=obj).filter(
                Q(status="done") | Q(completed_event__isnull=False))
        if done_refs.exists():
            raise ValidationError(
                "Delete blocked: completed work references this "
                "equipment.")


def validate_commands(actor, farm, raw_commands):
    """Parse, permission-check and dry-run every command in order."""
    if not isinstance(raw_commands, list) or not raw_commands:
        raise CommandError("Commands must be a non-empty list.")
    if len(raw_commands) > 50:
        raise CommandError("Too many commands in one changeset.")
    parsed = [parse_command(c) for c in raw_commands]
    seen_refs = set()
    for cmd in parsed:
        if cmd["ref"]:
            if cmd["ref"] in seen_refs:
                raise CommandError(
                    f"Duplicate ref: {cmd['ref']!r}")
            seen_refs.add(cmd["ref"])
        check_permission(actor, farm, cmd["target"], op=cmd["op"])
    for cmd in parsed:
        # Actor-scoped adapters resolve the actor from the prepared
        # store; untrusted actor fields are never read from commands.
        validate_one(farm, cmd, actor=actor)
    return parsed


def execute_commands(farm, parsed, actor=None):
    """Run validated commands; returns per-command result dicts.

    Every delete result carries the pre-delete ``impact`` enumeration
    and the exact ``related_writes`` applied alongside the delete
    (SET_NULL work items by id with before/after, deleted link rows).
    """
    from portal.domain_forms import EquipmentForm
    from portal.forms import ActionForm
    results = []
    for cmd in parsed:
        target, op = cmd["target"], cmd["op"]
        before = snapshot_one(farm, target, cmd["id"], actor=actor) \
            if op != "create" else None
        if target in _adapters.NEW_TARGETS:
            pk, after, extra = _adapters.execute(farm, cmd, actor)
        elif target == "action":
            pk, after, extra = _exec_action(farm, cmd)
        elif target == "equipment":
            pk, after, extra = _exec_equipment(farm, cmd)
        else:
            pk, after, extra = _exec_work_item(farm, cmd)
        results.append({"target": target, "op": op, "ref": cmd["ref"],
                        "pk": pk, "before": before, "after": after,
                        "impact": (extra or {}).get("impact"),
                        "related_writes": (extra or {}).get(
                            "related_writes", [])})
    return results


def preview_impacts(farm, parsed, actor=None):
    """Per-delete impact preview with exact related IDs; writes nothing.

    Used at prepare time (bound into the payload hash) and re-checked
    at apply time, so the approved package shows every related change
    before it happens.
    """
    from portal.models import WorkItem
    out = []
    for cmd in parsed:
        if cmd["op"] != "delete":
            continue
        obj = _scoped_qs(cmd["target"], farm, actor).filter(
            pk=cmd["id"]).first()
        if obj is None:
            continue
        impact = delete_impact(obj)
        entry = {"target": cmd["target"], "id": cmd["id"],
                 "cascades": impact.get("cascades", []),
                 "set_null": impact.get("set_null", [])}
        if cmd["target"] == "equipment":
            entry["work_items"] = sorted(
                WorkItem.objects.filter(
                    farm=farm, equipment=obj).values_list(
                        "pk", flat=True))
            entry["property_links"] = sorted(
                obj.property_links.values_list("pk", flat=True))
        out.append(entry)
    return out


def _exec_action(farm, cmd):
    from portal.forms import ActionForm
    from portal.models import FarmAction
    if cmd["op"] == "create":
        form = ActionForm(data={"text": cmd["fields"].get("text", "")})
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.instance
        obj.farm = farm
        obj.status = "open"
        obj.full_clean(exclude=["farm"])
        obj.save()
        return obj.pk, _dump_action(obj), {}
    obj = FarmAction.objects.get(pk=cmd["id"], farm=farm)
    if cmd["op"] == "delete":
        impact = delete_impact(obj)
        _enforce_delete_policy("action", obj, impact)
        pk = obj.pk
        obj.delete()
        return pk, None, {"impact": impact, "related_writes": []}
    data = {"text": cmd["fields"].get("text", obj.text)}
    form = ActionForm(data=data, instance=obj)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.save(commit=False)
    if "status" in cmd["fields"]:
        if cmd["fields"]["status"] not in ("open", "done"):
            raise ValidationError({"status": "Unknown status."})
        tmp.status = cmd["fields"]["status"]
        from django.utils import timezone
        tmp.done_at = timezone.now() if tmp.status == "done" else None
    tmp.farm = farm
    tmp.full_clean(exclude=["farm"])
    tmp.save()
    tmp.refresh_from_db()
    return tmp.pk, _dump_action(tmp), {}


def _exec_equipment(farm, cmd):
    from portal.domain_forms import EquipmentForm
    from portal.models import Equipment
    if cmd["op"] == "create":
        form = EquipmentForm(data=_equipment_form_data(cmd["fields"]),
                             farm=farm)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        obj = form.save(commit=False)
        obj.farm = farm
        obj.full_clean(exclude=["farm"])
        obj.save()
        form._save_m2m_through(
            obj, list(form.cleaned_data.get("properties") or []))
        obj.refresh_from_db()
        return obj.pk, _dump_equipment(obj), {}
    obj = Equipment.objects.get(pk=cmd["id"], farm=farm)
    if cmd["op"] == "delete":
        impact = delete_impact(obj)
        _enforce_delete_policy("equipment", obj, impact)
        from portal.models import WorkItem
        pk = obj.pk
        wi_ids = sorted(WorkItem.objects.filter(
            farm=farm, equipment=obj).values_list("pk", flat=True))
        link_ids = sorted(
            obj.property_links.values_list("pk", flat=True))
        obj.delete()
        related = [{"model": "portal.EquipmentProperty", "id": lid,
                    "deleted": True} for lid in link_ids]
        for wid in wi_ids:
            live = WorkItem.objects.filter(pk=wid).values_list(
                "equipment_id", flat=True).first()
            related.append({"model": "portal.WorkItem", "id": wid,
                            "field": "equipment", "before": pk,
                            "after": live})
        entry = {"target": "equipment", "id": pk,
                 "cascades": impact.get("cascades", []),
                 "set_null": impact.get("set_null", []),
                 "work_items": wi_ids, "property_links": link_ids}
        return pk, None, {"impact": entry, "related_writes": related}
    form = EquipmentForm(data=_equipment_form_data(cmd["fields"], base=obj),
                         instance=obj, farm=farm)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    tmp = form.save(commit=False)
    tmp.farm = farm
    tmp.full_clean(exclude=["farm"])
    tmp.save()
    form._save_m2m_through(
        tmp, list(form.cleaned_data.get("properties") or []))
    tmp.refresh_from_db()
    return tmp.pk, _dump_equipment(tmp), {}



def _exec_work_item(farm, cmd):
    from portal.models import WorkItem
    if cmd["op"] == "create":
        obj = _build_work_item(farm, cmd["fields"])
        obj.full_clean()
        _validate_work_item_Guards(obj, True)
        obj.save()
        obj.refresh_from_db()
        return obj.pk, _dump_work_item(obj), {}
    obj = WorkItem.objects.get(pk=cmd["id"], farm=farm)
    _validate_work_item_Guards(obj, False)
    if cmd["op"] == "delete":
        impact = delete_impact(obj)
        _enforce_delete_policy("work_item", obj, impact)
        pk = obj.pk
        obj.delete()
        entry = {"target": "work_item", "id": pk,
                 "cascades": impact.get("cascades", []),
                 "set_null": impact.get("set_null", [])}
        return pk, None, {"impact": entry, "related_writes": []}
    obj = _build_work_item(farm, cmd["fields"], instance=obj)
    obj.full_clean()
    _validate_work_item_Guards(obj, False)
    obj.save()
    obj.refresh_from_db()
    return obj.pk, _dump_work_item(obj), {}


def _dump_action(obj):
    return {"id": obj.pk, "text": obj.text, "status": obj.status}


def _dump_equipment(obj):
    try:
        props = sorted(obj.properties.values_list("pk", flat=True))
    except Exception:
        props = []
    return {"id": obj.pk, "name": obj.name,
            "capabilities": list(obj.capabilities or []),
            "available": bool(obj.available),
            "hours_per_day": str(obj.hours_per_day),
            "properties": props}


def _dump_work_item(obj):
    return {"id": obj.pk, "title": obj.title,
            "capability": obj.capability,
            "start_date": str(obj.start_date),
            "end_date": str(obj.end_date),
            "estimated_hours": str(obj.estimated_hours),
            "property": obj.property_id, "crop_cycle": obj.crop_cycle_id,
            "equipment": obj.equipment_id, "status": obj.status}


def snapshot_one(farm, target, pk, actor=None):
    target = canonical_target(target)
    if pk is None:
        return None
    if target in _adapters.NEW_TARGETS:
        try:
            row = _scoped_qs(target, farm, actor).get(pk=pk)
        except Exception:
            return None
        return _adapters.dump(target, row)
    try:
        row = _scoped_qs(target, farm).get(pk=pk)
    except Exception:
        return None
    if target == "action":
        return _dump_action(row)
    if target == "equipment":
        return _dump_equipment(row)
    return _dump_work_item(row)


def snapshot_farm(farm, actor=None):
    """Entire in-scope model snapshot with explicit defaults.

    Adapter targets append explicitly sorted snapshots; owner-scoped
    rows (rule categories/definitions) scope to the actor when given,
    otherwise to the farm owner — never to the farm owner on behalf
    of another actor.
    """
    from portal.models import Equipment, FarmAction, WorkItem
    actions = [_dump_action(o) for o in
               FarmAction.objects.filter(farm=farm).order_by("pk")]
    equip = [_dump_equipment(o) for o in
             Equipment.objects.filter(farm=farm).order_by("pk")
             .prefetch_related("properties")]
    items = [_dump_work_item(o) for o in
             WorkItem.objects.filter(farm=farm).order_by("pk")]
    out = {"action": actions, "equipment": equip, "work_item": items}
    for target in _adapters.NEW_TARGETS:
        out[target] = _adapters.snapshot_rows(target, farm, actor)
    return out


def snapshot_hash(snapshot):
    blob = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                      default=str)
    import hashlib
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
