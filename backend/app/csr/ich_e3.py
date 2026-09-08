"""The built-in ICH E3 structure: every numbered section a CSR carries.

This is seed data, not configuration a request can change -- the E3 numbering
IS the document. Each entry is (number, title, guidance, container): container
sections (9, 11.4, ...) are headings whose prose lives in their children; only
non-containers ever hold a draft. Guidance is what the section must contain,
written for both the template panel and the generation prompt.

DEFAULT_SOURCE_MAP is the editable half: which uploaded document types each
section retrieves from (spec §4). Sections absent from the map fall back to
every non-style-reference type.
"""

#: (section_number, title, guidance, is_container)
SECTIONS = (
    ("1", "Title Page", "Study identifiers: title, protocol number, compound, sponsor, phase, dates, confidentiality statement.", False),
    ("2", "Synopsis", "A brief (usually 3-10 page) summary of the whole study: objectives, design, population, treatments, endpoints, headline efficacy and safety results, conclusions.", False),
    ("3", "Table of Contents", "Generated at export time from the assembled document.", False),
    ("4", "List of Abbreviations and Definitions of Terms", "Every abbreviation used in the report with its expansion; fed by the QC abbreviation builder.", False),
    ("5", "Ethics", "", True),
    ("5.1", "Independent Ethics Committee or Institutional Review Board", "Confirmation that the protocol and amendments were reviewed by an IEC/IRB; list or reference of committees.", False),
    ("5.2", "Ethical Conduct of the Study", "Statement that the study was conducted per GCP and the Declaration of Helsinki.", False),
    ("5.3", "Patient Information and Consent", "How and when informed consent was obtained; reference the ICF.", False),
    ("6", "Investigators and Study Administrative Structure", "Investigators, sites, steering/monitoring committees, CROs, central laboratories and their responsibilities.", False),
    ("7", "Introduction", "Brief background: the compound, the indication, prior findings (from the IB), rationale for this study and its place in the development programme.", False),
    ("8", "Study Objectives", "The primary and secondary objectives exactly as the protocol states them.", False),
    ("9", "Investigational Plan", "", True),
    ("9.1", "Overall Study Design and Plan — Description", "Design (parallel/crossover), randomization, blinding, treatment arms, duration and sequence of study periods, visits.", False),
    ("9.2", "Discussion of Study Design, including the Choice of Control Groups", "Why this design and comparator; known limitations of the choices.", False),
    ("9.3", "Selection of Study Population", "", True),
    ("9.3.1", "Inclusion Criteria", "The inclusion criteria as the protocol states them.", False),
    ("9.3.2", "Exclusion Criteria", "The exclusion criteria as the protocol states them.", False),
    ("9.3.3", "Removal of Patients from Therapy or Assessment", "Rules for discontinuation and withdrawal, and how withdrawn patients were followed.", False),
    ("9.4", "Treatments", "", True),
    ("9.4.1", "Treatments Administered", "Exact treatments given in each arm: drug, dose, route, regimen.", False),
    ("9.4.2", "Identity of Investigational Product(s)", "Formulation, strength, batch/lot numbers, supplier.", False),
    ("9.4.3", "Method of Assigning Patients to Treatment Groups", "Randomization method, stratification, allocation concealment.", False),
    ("9.4.4", "Selection of Doses in the Study", "Rationale for the doses chosen, with reference to earlier studies.", False),
    ("9.4.5", "Selection and Timing of Dose for Each Patient", "Dosing schedule, timing rules, dose modifications allowed.", False),
    ("9.4.6", "Blinding", "Blinding method, who was blinded, circumstances for unblinding, emergency code-break procedure.", False),
    ("9.4.7", "Prior and Concomitant Therapy", "Which prior/concomitant medications were allowed or prohibited, and how they were recorded.", False),
    ("9.4.8", "Treatment Compliance", "How compliance was measured (counts, diaries, levels) and enforced.", False),
    ("9.5", "Efficacy and Safety Variables", "Primary/secondary efficacy endpoints and safety variables, and the schedule of their assessment.", False),
    ("9.6", "Data Quality Assurance", "Monitoring, audits, data management and validation steps taken to assure quality.", False),
    ("9.7", "Statistical Methods Planned in the Protocol and Determination of Sample Size", "Analysis populations, statistical models and tests per the SAP; sample-size calculation with its assumptions.", False),
    ("9.8", "Changes in the Conduct of the Study or Planned Analyses", "Protocol amendments and any deviation from planned analyses, each with its reason and timing relative to unblinding.", False),
    ("10", "Study Patients", "", True),
    ("10.1", "Disposition of Patients", "Numbers screened, randomized, treated, completed, discontinued with reasons, per arm -- from the disposition tables.", False),
    ("10.2", "Protocol Deviations", "Important deviations by category and arm, and how they were handled in analysis.", False),
    ("11", "Efficacy Evaluation", "", True),
    ("11.1", "Data Sets Analysed", "The analysis populations (ITT/FAS, PP, safety) with the number of patients in each and reasons for exclusion.", False),
    ("11.2", "Demographic and Other Baseline Characteristics", "Demographics and baseline disease characteristics per arm, from the baseline tables.", False),
    ("11.3", "Measurements of Treatment Compliance", "Observed compliance/exposure relevant to efficacy interpretation.", False),
    ("11.4", "Efficacy Results and Tabulations of Individual Patient Data", "", True),
    ("11.4.1", "Analysis of Efficacy", "Results for primary and secondary endpoints: estimates, CIs, p-values, per the SAP's methods.", False),
    ("11.4.2", "Statistical/Analytical Issues", "Covariates, dropout handling, interim analyses, multiplicity, subgroups -- issues and how they were addressed.", False),
    ("11.4.3", "Tabulation of Individual Response Data", "Reference to individual response listings where required.", False),
    ("11.4.4", "Drug Dose, Drug Concentration, and Relationships to Response", "Dose/exposure-response findings where studied.", False),
    ("11.4.5", "Drug-Drug and Drug-Disease Interactions", "Interaction findings where studied.", False),
    ("11.4.6", "By-Patient Displays", "Reference to any by-patient graphical displays.", False),
    ("11.4.7", "Efficacy Conclusions", "Short conclusions strictly limited to what the efficacy results showed.", False),
    ("12", "Safety Evaluation", "", True),
    ("12.1", "Extent of Exposure", "Duration and dose of exposure per arm, from the exposure tables.", False),
    ("12.2", "Adverse Events (AEs)", "", True),
    ("12.2.1", "Brief Summary of Adverse Events", "Overall AE incidence per arm: any AE, related, severe, serious, leading to discontinuation, deaths.", False),
    ("12.2.2", "Display of Adverse Events", "AEs by system organ class and preferred term per arm, from the AE tables.", False),
    ("12.2.3", "Analysis of Adverse Events", "Comparison of AE rates across arms; dose or time relationships where analysed.", False),
    ("12.2.4", "Listing of Adverse Events by Patient", "Reference to the by-patient AE listings.", False),
    ("12.3", "Deaths, Other Serious Adverse Events, and Other Significant Adverse Events", "Each death and SAE with a brief account drawn from the safety narratives; other significant AEs.", False),
    ("12.4", "Clinical Laboratory Evaluation", "Laboratory findings: shifts, notable abnormalities, per arm.", False),
    ("12.5", "Vital Signs, Physical Findings, and Other Observations Related to Safety", "Vital signs, ECG and physical findings relevant to safety.", False),
    ("12.6", "Safety Conclusions", "Short conclusions strictly limited to what the safety results showed.", False),
    ("13", "Discussion and Overall Conclusions", "Efficacy and safety findings discussed together, in the context of the design's limitations; overall benefit-risk conclusion. May draw on approved drafts of Sections 11 and 12.", False),
    ("14", "Tables, Figures and Graphs Referred to but Not Included in the Text", "The list of post-text tables/figures/listings referenced by the report.", False),
    ("15", "Reference List", "Publications cited in the report.", False),
    ("16", "Appendices", "The appendix inventory (protocol, SAP, ICF sample, ...).", False),
)

#: Which uploaded document types each section retrieves from (spec §4).
#: A prefix key ("9.4") covers its children unless a deeper key overrides it
#: ("9.4.6"). Sections not covered fall back to every citable type.
DEFAULT_SOURCE_MAP = {
    "2": ["protocol", "sap", "tlf"],
    "5": ["protocol", "icf"],
    "6": ["protocol"],
    "7": ["protocol", "ib"],
    "8": ["protocol"],
    "9": ["protocol"],
    "9.4.6": ["protocol", "randomization"],
    "9.7": ["sap"],
    "9.8": ["protocol", "sap"],
    "10": ["tlf", "protocol"],
    "11": ["sap", "tlf"],
    "12": ["tlf", "narrative"],
    "13": ["protocol", "sap", "tlf"],
    "14": ["tlf"],
}


def source_types_for(section_number: str) -> list:
    """The doc types a section retrieves from: its own entry, else the nearest
    prefix entry, else everything citable."""
    parts = section_number.split(".")
    for depth in range(len(parts), 0, -1):
        key = ".".join(parts[:depth])
        if key in DEFAULT_SOURCE_MAP:
            return list(DEFAULT_SOURCE_MAP[key])
    return []  # empty means "no filter": every citable doc type


def seed_sections() -> list:
    """Rows for csr_sections, in document order."""
    return [
        {"section_number": number, "title": title, "guidance_text": guidance,
         "is_container": container, "sort_order": index}
        for index, (number, title, guidance, container) in enumerate(SECTIONS)
    ]
