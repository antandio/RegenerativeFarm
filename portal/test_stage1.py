"""Stage1 TDD: active-farm scope, roles, invitations, audit (RED first)."""
import hashlib, re
from datetime import timedelta
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from portal.models import Farm, Membership, Invitation, AuditEvent, FarmAction

U = get_user_model()
PW = "Secret123!"

def mkuser(email):
    return U.objects.create_user(email=email, password=PW)

class Stage1Test(TestCase):
    def setUp(self):
        self.owner = mkuser("owner@x.hu")
        self.farm = Farm.objects.create(owner=self.owner, name="A farm")
    def login(self, u):
        self.client.force_login(u)

    def test_unauthorized_active_farm_selection_404(self):
        b = mkuser("b@x.hu"); Farm.objects.create(owner=b, name="B farm")
        self.login(b)
        r = self.client.post("/aktiv-farm/", {"farm_id": self.farm.id})
        self.assertEqual(r.status_code, 404)
        self.assertNotEqual(self.client.session.get("active_farm_id"), self.farm.id)

    def test_member_select_then_viewer_action_post_forbidden(self):
        v = mkuser("v@x.hu"); Farm.objects.create(owner=v, name="V farm")
        Membership.objects.create(farm=self.farm, user=v, role="viewer", is_active=True)
        self.login(v)
        r = self.client.post("/aktiv-farm/", {"farm_id": self.farm.id})
        self.assertEqual(r.status_code, 302)
        r = self.client.get("/attekintes/")
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/teendok/uj/", {"text": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 0)

    def _invite(self, email="w@x.hu", role="worker"):
        self.login(self.owner)
        r = self.client.post("/tagok/meghivas/", {"email": email, "role": role})
        self.assertEqual(r.status_code, 201)
        m = re.search(r"/tagok/elfogadas/([A-Za-z0-9\-_]+)/", r.content.decode())
        self.assertTrue(m, "accept link shown locally after create")
        return m.group(1)

    def test_invitation_accept_and_replay(self):
        tok = self._invite()
        w = mkuser("w@x.hu")
        self.login(w)
        r = self.client.post(f"/tagok/elfogadas/{tok}/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Membership.objects.filter(farm=self.farm, user=w, role="worker").exists())
        inv = Invitation.objects.get(farm=self.farm, email="w@x.hu")
        self.assertIsNotNone(inv.used_at)
        r = self.client.post(f"/tagok/elfogadas/{tok}/")
        self.assertEqual(r.status_code, 404)

    def test_invitation_wrong_email_forbidden(self):
        tok = self._invite(email="w@x.hu")
        other = mkuser("other@x.hu")
        self.login(other)
        r = self.client.post(f"/tagok/elfogadas/{tok}/")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Membership.objects.filter(farm=self.farm, user=other).exists())

    def test_invitation_expiry_404(self):
        tok = self._invite(email="e@x.hu")
        th = hashlib.sha256(tok.encode()).hexdigest()
        inv = Invitation.objects.get(token_hash=th)
        inv.expires_at = timezone.now() - timedelta(hours=1)
        inv.save(update_fields=["expires_at"])
        u = mkuser("e@x.hu")
        self.login(u)
        r = self.client.post(f"/tagok/elfogadas/{tok}/")
        self.assertEqual(r.status_code, 404)

    def test_invitation_revoke_404(self):
        tok = self._invite(email="r@x.hu")
        th = hashlib.sha256(tok.encode()).hexdigest()
        inv = Invitation.objects.get(token_hash=th)
        self.login(self.owner)
        r = self.client.post(f"/tagok/{inv.id}/visszavonas/")
        self.assertEqual(r.status_code, 302)
        u = mkuser("r@x.hu")
        self.login(u)
        r = self.client.post(f"/tagok/elfogadas/{tok}/")
        self.assertEqual(r.status_code, 404)

    def test_worker_cannot_request_advice(self):
        w = mkuser("ww@x.hu"); Farm.objects.create(owner=w, name="W farm")
        Membership.objects.create(farm=self.farm, user=w, role="worker", is_active=True)
        self.login(w)
        self.client.post("/aktiv-farm/", {"farm_id": self.farm.id})
        r = self.client.post("/tanacs/keres/", {"idempotency_key": "k1"})
        self.assertEqual(r.status_code, 403)

    def test_account_deletion_boundary(self):
        m = mkuser("m@x.hu"); mf = Farm.objects.create(owner=m, name="M farm")
        Membership.objects.create(farm=self.farm, user=m, role="worker", is_active=True)
        self.login(self.owner)
        r = self.client.post("/fiok/torles/", {"confirm_text": "TORLES"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Farm.objects.filter(pk=self.farm.pk).exists())
        self.assertFalse(Membership.objects.filter(user=m, farm=self.farm).exists())
        m.refresh_from_db()
        self.assertTrue(Farm.objects.filter(pk=mf.pk).exists())

    def test_audit_immutable(self):
        ev = AuditEvent.objects.create(farm=self.farm, actor=self.owner, action="test", object_type="x", object_id="1", meta={})
        ev.action = "changed"
        with self.assertRaises(ValueError):
            ev.save()
        with self.assertRaises(ValueError):
            ev.delete()
