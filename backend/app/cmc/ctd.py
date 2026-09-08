"""The CTD Module 3.2 section trees, as ICH M4Q numbers them.

Seed data, not configuration a request can change: the numbering IS the
dossier, and a reviewer looking for 3.2.P.5.1 will look for exactly that.
Each entry is (section_code, title, guidance, is_container). Containers are
headings whose prose lives in their children; only leaves ever hold a draft.

The guidance is written for two readers at once -- the person choosing which
sections apply, and the drafting prompt, which is handed it verbatim as
"TEMPLATE GUIDANCE FOR THIS SECTION". It says what the section must contain,
never how to phrase it.

`DATA_SECTIONS` is the half that makes this module different from a narrative
one: these sections carry a table built from verified structured data rather
than from anything a model writes. The mapping names which builder, so a
section cannot silently acquire prose where a specification belongs.
"""

#: (section_code, title, guidance, is_container)
DRUG_SUBSTANCE = (
    ("S.1", "General Information", "", True),
    ("S.1.1", "Nomenclature",
     "Recommended INN, compendial name, chemical name(s), company or laboratory code, "
     "CAS registry number, and other non-proprietary names.", False),
    ("S.1.2", "Structure",
     "Structural formula including relative and absolute stereochemistry, molecular "
     "formula, and relative molecular mass.", False),
    ("S.1.3", "General Properties",
     "Physicochemical and other relevant properties: physical description, solubilities "
     "in common solvents, pH and pKa values, polymorphism, partition coefficient, "
     "melting point, refractive index.", False),

    ("S.2", "Manufacture", "", True),
    ("S.2.1", "Manufacturer(s)",
     "Name, address and responsibility of each manufacturer, including contractors, and "
     "each production site or facility involved in manufacturing and testing.", False),
    ("S.2.2", "Description of Manufacturing Process and Process Controls",
     "The manufacturing process as a flow diagram and a narrative: starting materials, "
     "solvents, reagents and catalysts, the sequence of steps, operating conditions, "
     "in-process controls, and the batch size or range.", False),
    ("S.2.3", "Control of Materials",
     "Materials used in manufacture (starting materials, solvents, reagents, catalysts) "
     "with their quality and controls, and where each is tested.", False),
    ("S.2.4", "Controls of Critical Steps and Intermediates",
     "Tests and acceptance criteria at the critical steps identified in S.2.2, and the "
     "quality and control of isolated intermediates.", False),
    ("S.2.5", "Process Validation and/or Evaluation",
     "Process validation or evaluation studies for aseptic processing and sterilisation, "
     "and for any step where the outcome cannot be verified by testing the substance.", False),
    ("S.2.6", "Manufacturing Process Development",
     "The development history of the manufacturing process, changes made to batches used "
     "in nonclinical studies, clinical trials and stability, and the reasons for them.", False),

    ("S.3", "Characterisation", "", True),
    ("S.3.1", "Elucidation of Structure and other Characteristics",
     "Confirmation of structure from the route of synthesis and from spectral and "
     "analytical evidence; potential for isomerism, stereochemistry, polymorphism.", False),
    ("S.3.2", "Impurities",
     "Organic and inorganic impurities and residual solvents: their origin, structures "
     "where known, and the basis for the limits applied, per ICH Q3A/Q3C/Q3D and M7 as "
     "applicable.", False),

    ("S.4", "Control of Drug Substance", "", True),
    ("S.4.1", "Specification",
     "The specification: tests, analytical procedure references, and acceptance criteria "
     "at release and, where different, through the retest period.", False),
    ("S.4.2", "Analytical Procedures",
     "The analytical procedures used for each test in the specification, described in "
     "enough detail for another laboratory to perform them.", False),
    ("S.4.3", "Validation of Analytical Procedures",
     "Validation information for the analytical procedures, including experimental data, "
     "per ICH Q2 as applicable. Compendial procedures are verified rather than validated.", False),
    ("S.4.4", "Batch Analyses",
     "Batch analysis results for the batches used in development, nonclinical and "
     "clinical studies, and for any registration batches: batch number, size, date and "
     "site of manufacture, use, and the results obtained.", False),
    ("S.4.5", "Justification of Specification",
     "The rationale for the tests chosen and the acceptance criteria set, with reference "
     "to batch data, stability data, and any compendial requirement.", False),

    ("S.5", "Reference Standards or Materials",
     "Reference standards or materials used in testing: their source, characterisation, "
     "qualification and, for in-house standards, how they were established.", False),
    ("S.6", "Container Closure System",
     "The container closure system for storage and shipment: materials of construction "
     "of each primary component, their specifications, and their suitability.", False),

    ("S.7", "Stability", "", True),
    ("S.7.1", "Stability Summary and Conclusions",
     "The studies conducted, the protocols used, and the results, leading to the proposed "
     "retest period or shelf life and the storage conditions.", False),
    ("S.7.2", "Post-approval Stability Protocol and Stability Commitment",
     "The post-approval stability protocol and the commitment to continue or begin "
     "stability studies on production batches.", False),
    ("S.7.3", "Stability Data",
     "The stability results themselves, by batch, storage condition and timepoint, with "
     "the analytical procedures used to generate them.", False),
)

DRUG_PRODUCT = (
    ("P.1", "Description and Composition of the Drug Product",
     "The dosage form and its composition: each component, its amount per unit, its "
     "function, and the quality standard it meets; the container closure in brief; any "
     "accompanying reconstitution diluent.", False),

    ("P.2", "Pharmaceutical Development", "", True),
    ("P.2.1", "Components of the Drug Product", "", True),
    ("P.2.1.1", "Drug Substance",
     "The compatibility of the drug substance with the excipients, and the physicochemical "
     "characteristics of the substance that influence product performance.", False),
    ("P.2.1.2", "Excipients",
     "The choice of excipients, their concentrations and characteristics, and their "
     "influence on product performance.", False),
    ("P.2.2", "Drug Product", "", True),
    ("P.2.2.1", "Formulation Development",
     "The development of the formulation, including the batches used in clinical, "
     "bioavailability and bioequivalence studies, and any differences from the proposed "
     "commercial formulation.", False),
    ("P.2.2.2", "Overages",
     "Any overage in the formulation, with its justification.", False),
    ("P.2.2.3", "Physicochemical and Biological Properties",
     "Properties relevant to product performance: pH, ionic strength, dissolution, "
     "particle size, polymorphic form, rheology, sterility, and the like.", False),
    ("P.2.3", "Manufacturing Process Development",
     "The selection and optimisation of the manufacturing process, and any differences "
     "between the processes used for clinical batches and the commercial process.", False),
    ("P.2.4", "Container Closure System",
     "The suitability of the container closure system for storage, transport and use: "
     "protection, compatibility, safety and performance.", False),
    ("P.2.5", "Microbiological Attributes",
     "Microbiological attributes where relevant: the rationale for not performing "
     "microbial limits testing on a non-sterile product, the selection and effectiveness "
     "of a preservative system, the integrity of a sterile product's container.", False),
    ("P.2.6", "Compatibility",
     "Compatibility of the drug product with reconstitution diluents or dosage devices, "
     "to support the labelling.", False),

    ("P.3", "Manufacture", "", True),
    ("P.3.1", "Manufacturer(s)",
     "Name, address and responsibility of each manufacturer, including contractors, and "
     "each production site or facility involved in manufacturing and testing.", False),
    ("P.3.2", "Batch Formula",
     "The batch formula for the commercial batch size: every component, its quantity per "
     "batch, and its quality standard, including components removed during processing.", False),
    ("P.3.3", "Description of Manufacturing Process and Process Controls",
     "The manufacturing process as a flow diagram and a narrative, with the in-process "
     "controls, the equipment class, and the proposed batch size or range.", False),
    ("P.3.4", "Controls of Critical Steps and Intermediates",
     "Tests and acceptance criteria at the critical steps identified in P.3.3, and the "
     "quality and control of any isolated intermediate.", False),
    ("P.3.5", "Process Validation and/or Evaluation",
     "Process validation or evaluation for aseptic processing, sterilisation, and any "
     "step whose outcome cannot be verified by testing the finished product.", False),

    ("P.4", "Control of Excipients", "", True),
    ("P.4.1", "Specifications",
     "The specifications for each excipient: tests, procedure references and acceptance "
     "criteria, whether compendial or in-house.", False),
    ("P.4.2", "Analytical Procedures",
     "The analytical procedures used for excipients where these are not compendial.", False),
    ("P.4.3", "Validation of Analytical Procedures",
     "Validation of the non-compendial analytical procedures used for excipients.", False),
    ("P.4.4", "Justification of Specifications",
     "The rationale for any excipient specification that differs from the compendial one.", False),
    ("P.4.5", "Excipients of Human or Animal Origin",
     "For excipients of human or animal origin: source, specifications, testing, and the "
     "adventitious agents safety assessment (cross-refer to A.2).", False),
    ("P.4.6", "Novel Excipients",
     "For any excipient used for the first time in a drug product or by a new route: the "
     "manufacture, characterisation and controls, cross-referring to the safety data.", False),

    ("P.5", "Control of Drug Product", "", True),
    ("P.5.1", "Specification(s)",
     "The specification: tests, analytical procedure references, and acceptance criteria "
     "at release and through shelf life where these differ.", False),
    ("P.5.2", "Analytical Procedures",
     "The analytical procedures used for each test in the specification, described in "
     "enough detail for another laboratory to perform them.", False),
    ("P.5.3", "Validation of Analytical Procedures",
     "Validation information for the analytical procedures, including experimental data, "
     "per ICH Q2 as applicable.", False),
    ("P.5.4", "Batch Analyses",
     "Batch analysis results: batch number, size, date and site of manufacture, use, and "
     "the results obtained for each batch presented.", False),
    ("P.5.5", "Characterisation of Impurities",
     "Impurities and degradation products not already discussed in S.3.2, with the basis "
     "for their limits.", False),
    ("P.5.6", "Justification of Specification(s)",
     "The rationale for the tests chosen and the acceptance criteria set, with reference "
     "to batch data, stability data and any compendial requirement.", False),

    ("P.6", "Reference Standards or Materials",
     "Reference standards or materials used in testing the drug product, where not "
     "already described in S.5.", False),
    ("P.7", "Container Closure System",
     "The container closure system: the materials of construction of each primary "
     "component, its specification, and evidence of suitability.", False),

    ("P.8", "Stability", "", True),
    ("P.8.1", "Stability Summary and Conclusion",
     "The studies conducted, the protocols used, and the results, leading to the proposed "
     "shelf life, storage conditions and any in-use period.", False),
    ("P.8.2", "Post-approval Stability Protocol and Stability Commitment",
     "The post-approval stability protocol and the commitment to continue or begin "
     "stability studies on production batches.", False),
    ("P.8.3", "Stability Data",
     "The stability results themselves, by batch, storage condition and timepoint, with "
     "the analytical procedures used to generate them.", False),
)

APPENDICES_REGIONAL = (
    ("A.1", "Facilities and Equipment",
     "For biotechnological products: the facilities, equipment and procedures relevant to "
     "preventing contamination and cross-contamination.", False),
    ("A.2", "Adventitious Agents Safety Evaluation",
     "The assessment of viral and non-viral adventitious agents safety, including TSE/BSE "
     "statements for materials of animal origin.", False),
    ("A.3", "Novel Excipients",
     "Full manufacturing, characterisation and control information for a novel excipient, "
     "where one is used.", False),
    ("R.1", "Regional Information",
     "The region-specific items required by each target authority: executed batch records, "
     "method validation packages, comparability protocols, and the like. The required list "
     "depends on the regions selected for this project.", False),
)

#: Which sections carry a table built from verified structured data rather than
#: from prose. The value names the builder; the drafting prompt is told to emit
#: `[TABLE: <key>]` and write no numbers of its own.
DATA_SECTIONS = {
    "S.4.1": "spec_table",
    "S.4.4": "batch_analyses",
    "S.7.3": "stability_summary",
    "S.2.1": "site_list",
    "S.3.2": "impurity_table",
    "P.1": "composition_table",
    "P.3.1": "site_list",
    "P.3.2": "batch_formula",
    "P.5.1": "spec_table",
    "P.5.4": "batch_analyses",
    "P.5.5": "impurity_table",
    "P.8.3": "stability_summary",
}


def seed_sections(tree) -> list:
    """Rows for `cmc_sections`, in document order."""
    return [
        {"section_code": code, "title": title, "guidance_text": guidance,
         "is_container": container, "sort_order": index,
         "table_key": DATA_SECTIONS.get(code)}
        for index, (code, title, guidance, container) in enumerate(tree)
    ]
