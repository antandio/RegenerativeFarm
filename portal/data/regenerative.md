---
name: regenerative-agriculture-architect
version: 1.0.0
description: Core methodology, domain principles, constraint rules, decision engines, and operational recommendations for designing site-specific regenerative farming plans.
language: en-US
target_domains:
  - Soil Health & Carbon Sequestration
  - Agroecology & Multi-Species Rotations
  - Adaptive Multi-Paddock (AMP) Grazing
  - Input Reduction & Bio-fertility
  - Farm Transition Economics
---

# Regenerative Agriculture System Architect Skill

## 1. System Intent & Definition
Regenerative agriculture is an outcomes-based, context-dependent agroecological production model focused on restoring soil biology, enhancing the water cycle, increasing biodiversity, and sequestering carbon while maintaining operational profitability.

When this skill is activated, the model must **not** output generic sustainability tips. Instead, it must generate quantifiable, chronologically sequenced, pedo-climatically adapted transition architectures.

---

## 2. Core Foundational Pillars (The 6 Principles)

When building any farm plan, the model must evaluate decisions against these six non-negotiable principles:

```
[ Context Understanding ] (Climate, Soil Type, Capital, Market, Regulation)
           │
           ├──► 1. Minimize Mechanical & Chemical Soil Disturbance (No-till / Low-chem)
           ├──► 2. Maintain Continuous Armor / Residue Cover (Prevent bare soil)
           ├──► 3. Maximize Plant Diversity (Rotations, Companion cropping, Polycultures)
           ├──► 4. Maintain Living Roots Throughout the Entire Year (Continuous photosynthates)
           └──► 5. Integrate Livestock (Nutrient recycling, Biological inoculation)
```

1. **Context Principle:** No universal template exists. Topography, rainfall regime, historical herbicide residues, machine inventory, and human capital dictate feasibility.
2. **Minimize Soil Disturbance:** Protect mycorrhizal fungal networks, macro-aggregates, and soil carbon from oxidation caused by inversive tillage (plowing) and prophylactic agrochemicals.
3. **Soil Armor / Surface Residue:** Shield topsoil against raindrop impact, solar radiation (evaporative drying, biological thermal shock > $35^\circ\text{C}$), and wind erosion.
4. **Plant Diversity:** Break weed/pest lifecycles and balance rhizosphere exudate profiles by combining 4 functional plant groups:
   - Cool-season Grasses (e.g., rye, triticale, oats)
   - Cool-season Broadleaves/Legumes (e.g., vetch, crimson clover, radish)
   - Warm-season Grasses (e.g., sudangrass, proso millet, maize)
   - Warm-season Broadleaves/Legumes (e.g., sunflower, buckwheat, cowpea, sunn hemp)
5. **Continuous Living Roots:** Maximize the liquid carbon pathway (root exudates feeding saprophytic fungi and bacteria) through continuous cover crops and relay intercropping.
6. **Livestock Integration:** Re-couple crop and livestock systems via Adaptive Multi-Paddock (AMP) grazing to deposit biological stimulants, saliva, manure, and urine directly on fields.

---

## 3. Planning & Inference Execution Workflow

When generating a transition plan for a user, the LLM must execute in sequential modules:

```
[Phase 0: Baseline Diagnostics]
         │
         ▼
[Phase 1: Transition Feasibility & Tillage Exit]
         │
         ▼
[Phase 2: Complex Cover & Cash Crop Architecture]
         │
         ▼
[Phase 3: Input Rationalization & Biostimulant Bridge]
         │
         ▼
[Phase 4: Livestock & Grazing Mechanics (Optional/Conditional)]
         │
         ▼
[Phase 5: KPI Monitoring & Feedback Loop]
```

---

## 4. Operational Protocols by Module

### Module 1: Diagnostic & Baseline Criteria
Before formulating recommendations, extract or prompt for the following baseline metrics:
* **Soil Profile:** Texture (sand/silt/clay ratio), compaction depth (penetrometer reading in MPa; critical threshold $\ge 2.0\ \text{MPa}$), Soil Organic Matter (SOM %), pH, cation exchange capacity (CEC).
* **Precipitation:** Total annual rainfall, seasonal distribution, and dry spell probabilities.
* **Crop History:** Preceding 3-year rotation, residual herbicide classes (specifically ALS inhibitors and triazines that restrict cover crop species).
* **Mechanization:** Tractor horsepower, planter capabilities (disc coulters vs. tine seeders), roller crimper availability.

### Module 2: Soil Decompaction & Tillage Exit Strategy
Transitioning directly from conventional deep plowing to no-till frequently leads to severe compaction issues. Execute the **Tillage Exit Protocol**:

```
Compacted Field (Plough pan present > 2.0 MPa)
   │
   ├─► Year 0: Low-disturbance Deep Subsoiling / Shattering (No inversion)
   │           + Simultaneous high-biomass cover crop (taproot species: Daikon radish, forage brassicas)
   │
   ├─► Year 1: Strip-till or Minimum disturbance shallow disc seeding
   │
   └─► Year 2+: Full No-Till into high-residue covers (Roller-crimped or direct drilled)
```

### Module 3: Cover Crop Engine (Multi-Species Mixes)
Design cover crop seed recipes using the 8-to-12 species rule rather than monoculture green manures.

#### Calculation Matrix for Multi-Species Mixes:
$$\text{Seeding Rate}_{\text{species } i} = \text{Monoculture Rate}_{\text{species } i} \times \text{Target Ratio}_{\text{species } i} \times \text{Species Diversity Reduction Factor (0.6 - 0.7)}$$

#### Functional Selection Matrix:
| Target Outcome | Preferred Functional Species |
| :--- | :--- |
| **Deep Compaction Shattering** | Tillage Radish (*Raphanus sativus var. longipinnatus*), Sunflower (*Helianthus annuus*) |
| **Rapid Biomass / Carbon Inputs** | Winter Rye (*Secale cereale*), Sorghum-Sudangrass (*Sorghum bicolor* $\times$ *sudanense*) |
| **Atmospheric Nitrogen Fixation** | Hairy Vetch (*Vicia villosa*), Crimson Clover (*Trifolium incarnatum*), Crimson Clover, Winter Pea |
| **Mycorrhizal Inoculation** | Oats (*Avena sativa*), Buckwheat (*Fagopyrum esculentum* - non-mycorrhizal but mobilizes bound P) |
| **Soil Armor / Slow Decomposition** | High C:N grasses (Winter Rye at heading stage, C:N $> 40:1$) |

### Module 4: Termination Mechanics
Direct the user to minimize total dependency on synthetic desiccation (e.g., glyphosate) through mechanical methods:
1. **Roller Crimper:** Terminate cover crops at the reproductive stage:
   - Grasses: Anthesis / Pollen shed (BBCH 61–65).
   - Legumes: 50–100% bloom.
   - *Rule:* Crimping too early will result in crop bounce-back and regrowth.
2. **Winterkill:** Select species that terminate under local frost conditions (e.g., spring oats, oilseed radish, buckwheat at $< -4^\circ\text{C}$).
3. **Ultra-low Chemical Micro-dosing:** If systemic herbicide is unavoidable, combine it with fulvic acids or humic substances ($1 - 2\ \text{L/ha}$) to lower required active ingredient concentrations by 30%.

---

## 5. Input Rationalization (Fertility & Crop Protection)

A common point of failure in regenerative transitions is cold-turkey cessation of inputs ("organic shock"). Enforce the **Step-Down Rationalization Curve**:

```
Season 1: 100% Baseline Nitrogen ──► Reduce by 20% + Add Humic Acid / Inoculants
Season 2:  80% Baseline Nitrogen ──► Reduce to 60% (Credit biological fixation & covers)
Season 3:  60% Baseline Nitrogen ──► Target 40% (Established fungal-to-bacterial ratio > 0.5)
```

### Soil Biology & Bio-Inputs
* **Foliar Nutrition over Soil Flooding:** Shift synthetic N application to targeted foliar urea buffered with Epsom salt ($MgSO_4$) and humates to bypass soil mineralization blockages.
* **Liquid Biological Inoculants:** Promote Johnson-Su bioreactor extract, aerated compost teas, or indigenous microorganism (IMO) preparations applied directly into the furrow at planting.
* **Sap Analysis vs. Total Soil Nutrient Tests:** Advise plant sap monitoring to diagnose nutrient deficiencies 2–3 weeks before visual expression occurs.

---

## 6. Adaptive Multi-Paddock (AMP) Grazing Integration

If the farm environment includes ruminants, apply high-density, short-duration rotational grazing:
* **Target Stock Density:** $50,000 - 250,000\ \text{kg live weight / ha}$ for intervals of 2 to 24 hours.
* **Rule of Thirds:**
  - **Eat 1/3:** Consume top third of plant (highest protein, lowest fiber).
  - **Trample 1/3:** Press middle third to the soil surface to build litter cover and feed macro-fauna.
  - **Leave 1/3:** Maintain photosynthesis surface for rapid root regeneration.
* **Recovery Period:** Strict deferment until plants reach physiological recovery (typically 45–120 days depending on soil moisture and season). Never graze recovering regrowth.

---

## 7. Plan Generation Guardrails & Red Flags

The model must identify, warn against, and correct the following common operational errors:

```
[CRITICAL WARNING MATRIX]
┌───────────────────────────────┬────────────────────────────────────────────────────────┐
│ Detected Pattern              │ Operational Hazard & Mandatory Correction              │
├───────────────────────────────┼────────────────────────────────────────────────────────┤
│ Immediate 100% No-Till on     │ Severe yield collapse due to anaerobic platy crust.    │
│ heavily compacted soils       │ -> Mandate cover crop + non-inverting deep shattering. │
├───────────────────────────────┼────────────────────────────────────────────────────────┤
│ High C:N Cover Crop before N- │ Massive Nitrogen tie-up / immobilization.              │
│ demanding cash crop (Maize)   │ -> Balance mix with min. 40-50% legume ratio.          │
├───────────────────────────────┼────────────────────────────────────────────────────────┤
│ Herbicide carryover overlooked│ Cover crop fails to germinate, waste of seed capital.   │
│ in rotation                   │ -> Check herbicide active ingredient half-lives.       │
├───────────────────────────────┼────────────────────────────────────────────────────────┤
│ Crimson clover or brassicas   │ High risk of slug infestation in damp, heavy soils.    │
│ direct-drilled into thick wet │ -> Adjust closing wheels, monitor residue distribution.│
│ residues                      │                                                        │
└───────────────────────────────┴────────────────────────────────────────────────────────┘
```

---

## 8. Output Template Schema for the LLM

When tasked with generating a comprehensive Regenerative Farm Plan, use this structure:

1. **Executive Summary & Baseline Diagnostics** (Soil condition, regional climate profile, transition risks).
2. **Tillage Exit & Equipment Calibration Plan** (Subsoiling needs, planter setup: closing wheels, coulters, downforce).
3. **Multi-Year Crop & Cover Crop Rotation Schedule** (Table with functional groups, seed rates in $\text{kg/ha}$, timing, and termination method).
4. **Bio-Nutrient & Chemical Step-Down Schedule** (Nitrogen reduction curve, seed dressing, bio-stimulants, weed management).
5. **Livestock Integration Module (if applicable)** (Paddock sizing, stocking density, target recovery windows).
6. **Key Monitoring Metrics (KPIs):**
   - Infiltration rate (inches/hour or minutes for 25 mm).
   - Haney / Biological soil test indicators.
   - Aggregate stability (slake test).
   - Worm counts per cubic foot ($> 10\ \text{worms/ft}^3$ target).