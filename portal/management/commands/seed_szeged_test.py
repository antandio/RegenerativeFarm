import secrets, string
from django.core.management.base import BaseCommand
from django.db import transaction
from portal.models import User, Farm, FarmProfileSnapshot, AdviceRun, AdviceStep, FarmAction, TermsAcceptance
from django.conf import settings

EMAIL = "szeged.teszt@example.invalid"
CRED_PATH_NAME = "szeged_test_credentials.txt"

# Fully-filled SZINTETIKUS profile. The two parcels exist only as text in
# the current profile fields (no schema expansion). Areas (5 ha + 3 ha)
# are explicitly illustrative fictional values, not real measurements.
# No laboratory data is claimed and no outcome is guaranteed.
FARM = {
    "name": "Szeged-környéki homoki tesztgazdaság (SZINTETIKUS)",
    "region_text": "Szeged környéke, Dél-Alföld, homokhátság szegélye (fiktív elhelyezkedés, szintetikus tesztadat)",
    "climate_text": "Kontinentális, meleg és száraz nyarak, kevés és kiszámíthatatlan csapadék, gyakori szél; a nyári aszály a meghatározó korlát (általános jellemzés, nem mért adat)",
    "farming_direction_text": "Extenzív juhlegeltetés homoki gyepeken, kis léptékű regeneratív gyep- és talajtakarási próbákkal",
    "soil_notes": (
        "Két fiktív parcella, kizárólag illusztratív méretekkel (nem valós mérés): "
        "„A” parcella: 5 ha homokos gyep, saját megfigyelés szerint gyorsan kiszáradó, vékony termőrétegű foltokkal; "
        "„B” parcella: 3 ha homokos, helyenként lazább szerkezetű gyep, szélnek kitettebb részekkel. "
        "Laboratóriumi talajvizsgálati eredmény egyik parcellára sincs megadva; a leírás csak saját, egyszerű megfigyelésen alapul."
    ),
    "water_notes": (
        "Víz a fő korlát: a homok kevés vizet tart meg, nyáron mindkét parcella gyorsan kiszáradhat. "
        "Állatok itatása korlátozott hozamú kútról/itatóról megoldott; a legelőparcellákon öntözés nincs és nem tervezett. "
        "Száraz időszakban az itatás és a legeltetési rend igazodik a rendelkezésre álló vízhez."
    ),
    "crops_livestock_notes": (
        "Állatállomány: 10 db juh (extenzív legeltetés, szintetikus tesztadat). "
        "Növényzet: az 5 ha-os „A” és a 3 ha-os „B” parcellán homoki gyep; szántóföldi növény nincs. "
        "Kis próbaterületen takarónövény/mulcs kipróbálása elképzelhető, eredménygarancia nélkül."
    ),
    "equipment_labour_notes": "Kézi munka és alapvető legelőgazdálkodási eszközök (villanypásztor, itatók); nagyobb gép nincs, bérmunka csak alkalmanként; munkaerő: 1 fő családi munka, korlátozott napi időkerettel",
    "goals_notes": "A gyep borítottságának megőrzése és javítása óvatos, szakaszos legeltetéssel; a homok vízmegtartásának segítése talajtakarással; kis, visszafordítható regeneratív próbák tapasztalatszerzésre, garantált eredmény nélkül",
    "constraints_notes": "Szűk költségkeret és kevés munkaerő; a vízhiány és az aszály a legnagyobb bizonytalanság. Csak kis léptékű, megfigyeléssel kísért lépések vállalhatók; legeltetési és takarási döntések előtt helyi agronómus/gyepszakértő egyeztetése javasolt",
}

STEPS = [
    ("Heti legelő- és víz-megfigyelés indítása (SZINTETIKUS, forrás: seed)",
     "Vezessen heti rögzített megfigyelést mindkét parcellán (5 ha és 3 ha): gyepborítás, kopasz foltok, itatók vízállása és a 10 juh legelési mintázata feljegyzése ugyanazokon a pontokon.",
     "A profil szerint a víz a fő korlát és mért adat nincs, ezért a rendszeres saját megfigyelés ad alapot bármely további lépéshez.",
     "A megfigyelés azonos pontokon, következetesen történik; ez minta-tanácslépés, nem élő modelltanács.",
     "Alacsony kockázat; fő kockázat a feljegyzések elmaradása.",
     None),
    ("Szakaszos legeltetés kis próbaszakasszal (SZINTETIKUS, forrás: seed)",
     "Jelöljön ki a villanypásztorral egy kis próbaszakaszt, és rövidebb legeltetés után hagyjon pihenőidőt; figyelje meg a gyep visszarendeződését a pihenő és a legelt részek összehasonlításával.",
     "A homokos gyep és a 10 juh kis állománya kis léptékű, visszafordítható próbát tesz lehetővé anélkül, hogy az egész területet kitenné a kockázatnak.",
     "Egy idény megfigyelése szükséges; eredmény nem garantált, a csapadék erősen befolyásolhatja.",
     "Túllegeltetés a próbaszakaszon is kerülendő; szárazságban a pihenőidő hosszabbítása mérlegelendő.",
     "Legeltetési rend kialakításához helyi gyepgazdálkodási szakember egyeztetése javasolt."),
    ("Talajtakarási próba kiszáradó foltokon (SZINTETIKUS, forrás: seed)",
     "A leginkább kiszáradó kopasz foltokon próbáljon ki kis felületen mulcsolást vagy takarónövényt, és jegyezze fel a talajfelszín nedvességérzetét és a borítás alakulását eső után.",
     "A homok kevés vizet tart meg, ezért a takarás hatása először kis, összehasonlítható foltokon vizsgálható meg biztonságosan.",
     "A próba csak saját megfigyelésre támaszkodik; laboratóriumi adat nélkül általános jellegű.",
     "Víz- és költségigényt előtte érdemes szakemberrel egyeztetni; száraz tavaszon a próba elhalasztható.",
     "Kérdéseivel agronómushoz vagy talajtani szakemberhez fordulhat."),
]

ACTIONS = [
    ("Heti megfigyelési napló elindítása mindkét parcellán (SZINTETIKUS minta)", "open"),
    ("Villanypásztor próbaszakasz kijelölése az „A” parcellán (SZINTETIKUS minta)", "open"),
    ("Itatók és kút állapotának ellenőrzése (SZINTETIKUS minta)", "done"),
]


class Command(BaseCommand):
    help = "Idempotens SZINTETIKUS Szeged-környéki tesztfarm (10 juh, 2 homoki parcella). Élő API-hívást soha nem végez; felhőhozzájárulást nem ad meg."

    def handle(self, *args, **options):
        created_pw = None
        with transaction.atomic():
            user, created = User.objects.get_or_create(email=EMAIL)
            if created:
                pw = "".join(secrets.choice(string.ascii_letters + string.digits + "!@#$%") for _ in range(24))
                user.set_password(pw)
                user.save()
                created_pw = pw
                TermsAcceptance.objects.get_or_create(
                    user=user, terms_version=settings.TERMS_VERSION,
                    privacy_notice_version=settings.PRIVACY_VERSION)
            farm, _ = Farm.objects.get_or_create(owner=user, defaults=dict(FARM))
            from portal.models import FarmLocation as _FL, FarmProperty as _FP, CropCycle as _CC
            loc, _ = _FL.objects.get_or_create(farm=farm, name="Szeged, központi telephely (SZINTETIKUS)", defaults={"latitude": "46.253000", "longitude": "20.141400", "timezone": "Europe/Budapest"})
            prop_a, _ = _FP.objects.get_or_create(farm=farm, name="A parcella (SZINTETIKUS, illusztratív)", defaults={"location": loc, "kind": "field", "area_hectares": "5.00", "notes": "Homoki gyep, illusztratív méret; nem valós mérés."})
            prop_b, _ = _FP.objects.get_or_create(farm=farm, name="B parcella (SZINTETIKUS, illusztratív)", defaults={"location": loc, "kind": "field", "area_hectares": "3.00", "notes": "Homoki gyep, illusztratív méret; nem valós mérés."})
            _CC.objects.get_or_create(farm=farm, property=prop_a, state="current", defaults={"crop_name": "Homoki gyep (SZINTETIKUS, illusztratív)", "growth_stage": "", "observations": "Jelenlegi állapot; 10 juh legel rajta. Illusztratív adat."})
            _CC.objects.get_or_create(farm=farm, property=prop_b, state="planned", defaults={"crop_name": "Takarónövény-próba (SZINTETIKUS, illusztratív)", "growth_stage": "", "observations": "Tervezett kis próba, eredménygarancia nélkül. Illusztratív adat."})
            # NOTE: cloud consent is deliberately NOT granted here. The
            # default is not-granted; the user may grant it via the UI.
            snap, _ = FarmProfileSnapshot.objects.get_or_create(
                farm=farm, profile_version=farm.profile_version,
                defaults={"profile_json": farm.profile_dict(), "profile_schema_version": 1})
            run, run_created = AdviceRun.objects.get_or_create(
                farm=farm, idempotency_key=f"seed-szeged-{farm.id}-v1",
                defaults={"initiated_by": user, "profile_snapshot": snap, "status": "success",
                          "source": "seed", "requested_model": "seed (nincs modellhívás)",
                          "actual_model": None,
                          "result_assumptions": ["SZINTETIKUS mintaadat (forrás: seed) – nem élő tanács, nem mért adatokon alapul"],
                          "result_focused_questions": ["Melyik parcella szárad ki hamarabb nyáron?", "Mekkora pihenőidő után rendeződik vissza a gyep a próbaszakaszon?"],
                          "result_is_general": True, "completed_at": farm.created_at})
            if run_created:
                for i, (t, a, r, asm, risk, ref) in enumerate(STEPS, start=1):
                    AdviceStep.objects.create(advice_run=run, farm=farm, position=i, title=t,
                                              action_text=a, rationale=r, assumptions=[asm],
                                              risk_note=risk, expert_referral=ref)
                for text, status in ACTIONS:
                    defaults = {"status": status}
                    if status == "done":
                        from django.utils import timezone
                        defaults["done_at"] = timezone.now()
                    first_step = run.steps.order_by("position").first()
                    if text.startswith("Heti megfigyel"):
                        defaults.update({"source_step": first_step, "source_step_farm_id": farm.id})
                    FarmAction.objects.get_or_create(farm=farm, text=text, defaults=defaults)
        import sys as _sys
        if created_pw is not None and "test" not in _sys.argv:
            import os
            path = os.path.join(settings.BASE_DIR, CRED_PATH_NAME)
            with open(path, "w") as f:
                f.write(f"{EMAIL}  {created_pw}\n")
            os.chmod(path, 0o600)
            self.stdout.write("Szeged teszt hitelesítő adat elmentve: szeged_test_credentials.txt (chmod 600). A jelszó nem kerül kimenetre.")
        self.stdout.write(self.style.SUCCESS(
            "Seed kész: 1 SZINTETIKUS szegedi tesztfarm (forrás: seed, élő hívás nélkül, felhőhozzájárulás nincs megadva). Ismételt futás nem dupláz és jelszót nem ír felül."))
