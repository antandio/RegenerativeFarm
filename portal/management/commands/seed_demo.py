import secrets, string
from django.core.management.base import BaseCommand
from django.db import transaction
from portal.models import User, Farm, FarmProfileSnapshot, AdviceRun, AdviceStep, FarmAction, ConsentEvent, TermsAcceptance
from django.conf import settings

FARMS = [
    {"email":"demo1@demo.pelda.hu","name":"Duna-völgyi mintagazdaság (SZINTETIKUS)","region_text":"Duna-völgy, löszös hát","climate_text":"Száraz nyár, kevés tavaszi csapadék","farming_direction_text":"Szántóföld és takarónövények","soil_notes":"Löszös, közepes kötöttség; saját megfigyelés: lassú beszivárgás","water_notes":"Kút korlátozott hozammal; öntözés nincs","crops_livestock_notes":"Búza, kukorica, napraforgó; állat nincs","equipment_labour_notes":"Saját traktor, bérmunka elérhető; 1 fő + idénymunka","goals_notes":"Talajborítás növelése, vízmegtartás","constraints_notes":"Szűk költségkeret; legnagyobb bizonytalanság a csapadék"},
    {"email":"demo2@demo.pelda.hu","name":"Dombvidéki mintalegelő (SZINTETIKUS)","region_text":"Dombvidék, agyagos lejtők","climate_text":"Változékony, eróziós esők","farming_direction_text":"Gyep és extenzív legeltetés","soil_notes":"Agyagos, erózióra hajlamos foltok","water_notes":"Forrás és itatók; nyári vízhiány előfordul","crops_livestock_notes":"Állandó gyep; kis létszámú szarvasmarha","equipment_labour_notes":"Kézi munka túlsúly; bérmunka ritka","goals_notes":"Erózió csökkentése, gyep állapotjavítás","constraints_notes":"Kevés munkaerő; kockázatkerülő"},
    {"email":"demo3@demo.pelda.hu","name":"Homoki mintakertészet (SZINTETIKUS)","region_text":"Homokhátság","climate_text":"Meleg, száraz, szeles","farming_direction_text":"Zöldség és takarónövény-kísérletek","soil_notes":"Homokos, alacsony szervesanyag (saját becslés)","water_notes":"Csepegtető öntözés korlátozott vízzel","crops_livestock_notes":"Vegyes zöldségágyások; állat nincs","equipment_labour_notes":"Kisgépek; családi munkaerő","goals_notes":"Szervesanyag és víztakarékosság növelése","constraints_notes":"Víz a fő korlát; kísérletező, de kis lépésekben"},
]
STEPS = [
    ("Heti megfigyelési rend indítása","Vezessen heti rögzített megfigyelést: talajnedvesség érzete, borítás és vízállapot feljegyzése ugyanazokon a pontokon.","A profil kevés mért adatot tartalmaz, ezért a saját rendszeres megfigyelés adja a megbízható alapot.","A megfigyelés következetesen, azonos pontokon történik.","Alacsony kockázat; fő kockázat a következetesség elmaradása.",None),
    ("Víz- és munkacsúcsok feltérképezése","Jegyezze fel egy idényre visszamenőleg a vízhiány és munkacsúcsok idejét, és jelölje a legszűkebb időszakokat.","A víz- és munkaerő-korlátok a profilban kifejezetten szerepelnek, ezért a sorrendet ezekhez érdemes igazítani.","Az emlékezeti feljegyzés közelítő; pontos mérés nélkül is irányt ad.","Közvetlen beavatkozás nélkül, visszafordítható lépés.",None),
    ("Kisléptékű takarási próba kijelölése","Jelöljön ki egy kis, összehasonlítható próbaterületet takarónövény vagy mulcs kipróbálására, és rögzítse az összehasonlítás szempontjait.","Kis területen a kockázat korlátozott, és a saját feltételek mellett szerezhető tapasztalat.","Egy idény megfigyelése szükséges az értékeléshez.","Víz- és költségigényt előtte érdemes szakemberrel egyeztetni.","Kérdéseivel agronómushoz vagy talajtani szakemberhez fordulhat."),
]

class Command(BaseCommand):
    help = "Idempotens szintetikus demo adatok (3 farm). Élő API-hívást soha nem végez."
    def handle(self, *args, **options):
        creds = []
        with transaction.atomic():
            for spec in FARMS:
                user, created = User.objects.get_or_create(email=spec["email"])
                if created:
                    pw = "".join(secrets.choice(string.ascii_letters + string.digits + "!@#$%") for _ in range(20))
                    user.set_password(pw); user.save()
                    creds.append((spec["email"], pw))
                    TermsAcceptance.objects.get_or_create(user=user, terms_version=settings.TERMS_VERSION, privacy_notice_version=settings.PRIVACY_VERSION)
                farm, _ = Farm.objects.get_or_create(owner=user, defaults={k: spec[k] for k in ["name","region_text","climate_text","farming_direction_text","soil_notes","water_notes","crops_livestock_notes","equipment_labour_notes","goals_notes","constraints_notes"]})
                ConsentEvent.objects.get_or_create(farm=farm, actor=user, event_type="granted", policy_version=settings.CLOUD_POLICY_VERSION)
                snap, _ = FarmProfileSnapshot.objects.get_or_create(farm=farm, profile_version=farm.profile_version, defaults={"profile_json": farm.profile_dict(), "profile_schema_version": 1})
                run, run_created = AdviceRun.objects.get_or_create(farm=farm, idempotency_key=f"seed-{farm.id}-v1", defaults={"initiated_by": user, "profile_snapshot": snap, "status": "success", "source": "seed", "requested_model": "seed (nincs modellhívás)", "actual_model": None, "result_assumptions": ["SZINTETIKUS mintaadat – nem élő tanács"], "result_focused_questions": ["Melyik parcella a leginkább vízhiányos?"], "result_is_general": True, "completed_at": farm.created_at})
                if run_created:
                    for i, (t, a, r, asm, risk, ref) in enumerate(STEPS, start=1):
                        AdviceStep.objects.create(advice_run=run, farm=farm, position=i, title=t, action_text=a, rationale=r, assumptions=[asm], risk_note=risk, expert_referral=ref)
                    FarmAction.objects.get_or_create(farm=farm, text="Heti megfigyelési napló elindítása (SZINTETIKUS minta)", defaults={"status": "open", "source_step": run.steps.order_by("position").first(), "source_step_farm_id": farm.id})
                    FarmAction.objects.get_or_create(farm=farm, text="Próbaterület kijelölése a térképen (SZINTETIKUS minta)", defaults={"status": "done"})
        import sys as _sys
        if creds and "test" not in _sys.argv:
            import os
            path = os.path.join(settings.BASE_DIR, "demo_credentials.txt")
            with open(path, "a") as f:
                for email, pw in creds:
                    f.write(f"{email}  {pw}\n")
            os.chmod(path, 0o600)
            self.stdout.write(f"Demo hitelesítő adatok hozzáfűzve: demo_credentials.txt (chmod 600). Jelszavak nem kerülnek kimenetre.")
        self.stdout.write(self.style.SUCCESS("Seed kész: 3 SZINTETIKUS farm (forrás: seed, élő hívás nélkül). Ismételt futás nem dupláz."))
