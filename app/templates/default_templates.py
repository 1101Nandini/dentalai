"""
Global default note templates seeded into the database for all organizations.
"""

DEFAULT_TEMPLATES = [
    {
        "procedure_name": "Comprehensive oral evaluation \u2014 new patient",
        "cdt_refs": ["D0150", "D0210"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}. BP {{bp}}. ASA {{asa || \"I\"}}.\n\nChief complaint: \"{{chief_complaint}}\" Comprehensive oral evaluation performed; full-mouth radiographic series taken and reviewed.\n\nHead, neck, TMJ, and lymph nodes WNL. Oral cancer screening: {{oral_cancer_screening || \"WNL\"}}. Remaining soft tissues pink, firm, and well-hydrated. Periodontal: {{periodontal_status || \"stable\"}}.\n\nHard tissue: {{hard_tissue_findings || \"no active carious lesions\"}}. Occlusion: {{occlusion || \"Class I\"}}.\n\nDiagnosis: {{diagnosis}}. Plan: {{plan}}. Reviewed with patient including risks, benefits, alternatives, and costs."
    },
    {
        "procedure_name": "Periodic oral evaluation \u2014 recall",
        "cdt_refs": ["D0120", "D0274"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}.\n\nPeriodic oral evaluation; patient presents for routine recall. Bitewing radiographs taken and reviewed.\n\nOral cancer screening WNL. Periodontal status stable, no significant change. Hard tissue: no new caries detected. No significant changes since last evaluation.\n\nRecall interval: {{recall_interval || \"6 months\"}}."
    },
    {
        "procedure_name": "Limited evaluation \u2014 palliative",
        "cdt_refs": ["D0140", "D9110"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}. BP {{bp}}.\n\nChief complaint: \"{{chief_complaint}}\" Onset/duration: {{duration}}. Pain {{pain_scale}}/10, {{pain_character}}.\n\nLimited (problem-focused) evaluation of #{{tooth_number}}. Findings: {{findings}}.\n\nPulp testing: {{pulp_testing}}. Periapical radiograph: {{radiograph_findings}}.\n\nDiagnosis: {{diagnosis}}. Prescribed {{prescription}}. Definitive treatment discussed; patient scheduled for {{planned_treatment}}."
    },
    {
        "procedure_name": "Adult prophylaxis",
        "cdt_refs": ["D1110", "D1206"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}.\n\nAdult prophylaxis performed. Supra- and subgingival scaling and polishing completed; {{calculus_level || \"light\"}} calculus and stain removed. Bleeding on probing: {{bleeding || \"none\"}}. Periodontal status: {{periodontal_status || \"healthy\"}}.\n\nOral hygiene: {{oral_hygiene || \"good\"}}. Brushing and flossing technique reviewed. {{#if fluoride}}Fluoride applied: {{fluoride_type || \"5% NaF varnish\"}}. Patient instructed not to eat or drink for 30 minutes.{{/if}}"
    },
    {
        "procedure_name": "Scaling & root planing",
        "cdt_refs": ["D4341", "D4342"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}. BP {{bp}}.\n\nScaling and root planing, {{quadrants}}. Local anesthesia: {{anesthesia}}, profound anesthesia achieved.\n\nModerate-to-heavy subgingival calculus and diseased cementum removed using ultrasonic and hand instrumentation; root surfaces planed smooth. Pre-treatment pocketing: {{pocketing}}. Hemostasis achieved.\n\nRe-evaluation scheduled in 4-6 weeks. Importance of meticulous home care and periodontal maintenance emphasized."
    },
    {
        "procedure_name": "Composite restoration",
        "cdt_refs": ["D2391", "D2392", "D2393", "D2394"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}. BP {{bp}}.\n\nComposite restoration, tooth #{{tooth_number}}, surfaces {{surfaces}}. Local anesthesia: {{anesthesia}}.\n\nTooth isolated with {{isolation || \"rubber dam\"}}. Caries excavated to sound dentin; preparation completed. Prep etched, rinsed, and dried; {{bonding_agent || \"universal bonding agent\"}} applied and light-cured. {{composite_material || \"Nanohybrid composite\"}}, shade {{shade}}, placed incrementally and light-cured. Restoration contoured and finished.\n\nOcclusion verified with articulating paper and adjusted; excursive movements checked. Restoration polished to high luster."
    },
    {
        "procedure_name": "Crown preparation",
        "cdt_refs": ["D2740", "D2950"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}. BP {{bp}}.\n\nCrown preparation, tooth #{{tooth_number}} ({{crown_type}}). Indication: {{indication}}. Local anesthesia: {{anesthesia}}.\n\nExisting restoration removed and core buildup placed using {{buildup_material || \"resin core material\"}}. Tooth prepared for {{crown_type}} crown with appropriate occlusal and axial reduction, smooth margins, and adequate taper.\n\nGingival retraction with {{retraction || \"cord\"}}. Final impression: {{impression || \"digital intraoral scan\"}}. Shade {{shade}}. Opposing and bite records captured. Provisional fabricated with {{temp_material || \"bis-acryl\"}} and cemented with {{temp_cement || \"temporary cement\"}}; occlusion and contacts verified. Case sent to lab."
    },
    {
        "procedure_name": "Crown delivery / seat",
        "cdt_refs": ["D2740"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}.\n\nDelivery of {{crown_type}} crown, tooth #{{tooth_number}}. Provisional removed and tooth cleaned of temporary cement.\n\nCrown tried in: marginal fit verified closed, interproximal contacts ideal. Occlusion verified in centric and excursions; adjusted as needed.\n\nCrown cemented with {{permanent_cement || \"resin-modified glass ionomer cement\"}}. Excess cement thoroughly removed and margins flossed clear. Final occlusion confirmed."
    },
    {
        "procedure_name": "Root canal therapy",
        "cdt_refs": ["D3310", "D3320", "D3330"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}. BP {{bp}}.\n\nRoot canal therapy, tooth #{{tooth_number}}. Diagnosis: {{diagnosis}}. Local anesthesia: {{anesthesia}}, profound anesthesia achieved.\n\nTooth isolated with {{isolation || \"rubber dam\"}}. Access cavity prepared; {{canals}} located and negotiated. Working lengths established with electronic apex locator and confirmed radiographically.\n\nCanals instrumented with {{instrumentation || \"rotary NiTi to appropriate taper\"}} and irrigated with {{irrigation || \"sodium hypochlorite and EDTA\"}}; dried with paper points. Obturated with {{obturation || \"gutta-percha and bioceramic sealer\"}} to length. Access sealed with {{temporary_restoration || \"temporary restoration\"}}.\n\nPlan: {{plan || \"core buildup and full-coverage crown recommended\"}}. Patient advised tooth requires definitive cuspal coverage."
    },
    {
        "procedure_name": "Simple extraction",
        "cdt_refs": ["D7140"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}. BP {{bp}}.\n\nExtraction of tooth #{{tooth_number}}. Indication: {{indication}}. Risks, benefits, and alternatives including replacement options discussed; informed consent obtained.\n\nLocal anesthesia: {{anesthesia}}, profound anesthesia confirmed.\n\nTooth luxated with elevators and delivered with forceps. Socket inspected and curetted; no retained root tips or pathology. Tooth removed intact without complication. Hemostasis achieved with pressure and gauze.\n\nPost-extraction instructions reviewed (bleeding control, no smoking/straws/spitting, soft diet, dry socket precautions)."
    },
    {
        "procedure_name": "Surgical extraction \u2014 impacted",
        "cdt_refs": ["D7210", "D7220", "D7230", "D7240"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}. BP {{bp}}.\n\nSurgical extraction of tooth #{{tooth_number}} ({{impaction_type}}). Risks including swelling, bleeding, infection, dry socket, and nerve involvement (paresthesia) discussed; informed surgical consent obtained.\n\nLocal anesthesia: {{anesthesia}}.\n\nFull-thickness mucoperiosteal flap reflected. Buccal bone removed with surgical handpiece under copious irrigation. Tooth sectioned and delivered in segments. Socket debrided and inspected; irrigated with sterile saline. Flap repositioned and closed with {{sutures || \"3-0 chromic gut sutures\"}}. Hemostasis achieved. Procedure completed without complication."
    },
    {
        "procedure_name": "Implant placement",
        "cdt_refs": ["D6010"],
        "video_url": None,
        "is_default": True,
        "body_template": "Medical history reviewed; no significant changes. Allergies: {{allergies || \"NKDA\"}}. BP {{bp}}.\n\nEndosteal implant placement, site #{{tooth_number}}. Surgical risks (infection, bleeding, nerve injury/paresthesia, implant failure, need for grafting) reviewed; informed consent obtained.\n\nLocal anesthesia: {{anesthesia}}.\n\nCrestal incision made and full-thickness flap reflected to expose the ridge. Osteotomy prepared sequentially under copious sterile irrigation. Implant {{implant_size}} placed to depth. Insertion torque: {{torque}} Ncm. Primary stability: {{stability || \"good\"}}. Healing abutment placed; flap closed with {{sutures || \"4-0 PTFE sutures\"}}.\n\nPost-op verification radiograph confirms position. Healing period and restorative timeline discussed. Patient advised on soft diet and avoidance of pressure at the surgical site."
    }
]
