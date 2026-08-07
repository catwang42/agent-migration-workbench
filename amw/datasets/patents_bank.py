"""The patents domain bank: the raw facts every template is built from.

Realism is the point of this file. The workshop audience are patent-search
people; a dataset of "find patents about batteries" would tell them nothing
about whether Gemini can do their job, and would make the whole scorecard easy
to dismiss. So the scenarios carry real CPC subgroups, field-appropriate
jargon, the units and magnitudes a person in that field would recognise, and
assignee strings in the form they actually appear on a patent front page
(``Kabushiki Kaisha``, ``Co., Ltd.``, ``GmbH``, ``(publ)``).

Two honesty notes, because this is synthetic data about a real-world corpus:

* The **CPC codes and technical vocabulary are real**; that is what makes the
  filter-matching metrics meaningful.
* The **patents are not**. Publication numbers, dates, claim text and abstracts
  are composed here. No item asserts anything about a real filing, and every
  item ships with ``provenance: synthetic`` (ground rule 2). Assignee strings
  mix real operating companies — which is what a real query log looks like,
  people search by the company they care about — with plausible invented
  smaller entities.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Tech", "TECHS", "ASSIGNEES", "JURISDICTION_PHRASES", "tech_by_key"]


@dataclass(frozen=True)
class Assignee:
    """An organisation as it would be printed on a front page."""

    name: str
    #: Two-letter office the entity most often files in first. Used to make
    #: jurisdiction constraints in questions plausible, never as a gold answer
    #: on its own — nationality is not jurisdiction, and an item that implied it
    #: was would be teaching the model to guess.
    home_office: str


@dataclass(frozen=True)
class Metric:
    """A measured property, the way a specification would state it."""

    name: str
    value: str
    context: str = ""

    def phrase(self) -> str:
        return f"{self.name} of {self.value}{(' ' + self.context) if self.context else ''}"


@dataclass(frozen=True)
class Tech:
    """One technology area, with everything a template needs to talk about it."""

    key: str
    #: Short noun phrase, suitable for PatentFeatures.technical_field.
    field_phrase: str
    #: How a searcher would refer to the area in conversation.
    colloquial: str
    #: Real CPC subgroups, most specific first.
    cpc: tuple[str, ...]
    #: Synonyms and near-terms a good rewritten query would expand into.
    terms: tuple[str, ...]
    #: The thing the independent claim is about.
    claim_subject: str
    #: Distinguishing feature the claim recites.
    claim_feature: str
    #: A second, dependent-claim-shaped refinement.
    claim_refinement: str
    #: Quantitative properties, for the extraction-heavy templates.
    metrics: tuple[Metric, ...]
    #: The problem the field is trying to solve — used for novelty statements.
    problem: str
    #: Assignee keys plausible in this field.
    assignees: tuple[str, ...] = field(default_factory=tuple)


ASSIGNEES: dict[str, Assignee] = {
    # Real operating companies. People search by these names.
    "toyota": Assignee("Toyota Jidosha Kabushiki Kaisha", "JP"),
    "samsung_sdi": Assignee("Samsung SDI Co., Ltd.", "KR"),
    "lges": Assignee("LG Energy Solution, Ltd.", "KR"),
    "catl": Assignee("Contemporary Amperex Technology Co., Limited", "CN"),
    "panasonic": Assignee("Panasonic Intellectual Property Management Co., Ltd.", "JP"),
    "bosch": Assignee("Robert Bosch GmbH", "DE"),
    "siemens": Assignee("Siemens Aktiengesellschaft", "DE"),
    "philips": Assignee("Koninklijke Philips N.V.", "NL"),
    "ericsson": Assignee("Telefonaktiebolaget LM Ericsson (publ)", "SE"),
    "huawei": Assignee("Huawei Technologies Co., Ltd.", "CN"),
    "qualcomm": Assignee("QUALCOMM Incorporated", "US"),
    "amat": Assignee("Applied Materials, Inc.", "US"),
    "basf": Assignee("BASF SE", "DE"),
    "broad": Assignee("The Broad Institute, Inc.", "US"),
    "genentech": Assignee("Genentech, Inc.", "US"),
    "quantumscape": Assignee("QuantumScape Battery, Inc.", "US"),
    "topsoe": Assignee("Topsoe A/S", "DK"),
    # Plausible invented smaller entities — a real query log is full of these.
    "helion": Assignee("Helion Elektrolyte GmbH", "DE"),
    "cathodica": Assignee("Cathodica Materials, Inc.", "US"),
    "lumenex": Assignee("Lumenex Photonics KK", "JP"),
    "aerio": Assignee("Aerio Thermal Systems Ltd", "GB"),
    "novastrand": Assignee("Novastrand Therapeutics, Inc.", "US"),
    "beamforge": Assignee("Beamforge Semiconductor Co., Ltd.", "TW"),
    "hanwoo": Assignee("Hanwoo Display Co., Ltd.", "KR"),
    "veriqubit": Assignee("Veriqubit Systems, Inc.", "US"),
}


#: How people phrase a jurisdiction in a real question, and what it actually
#: licenses as a filter. The ambiguous ones are the edge-case fuel.
JURISDICTION_PHRASES: dict[str, tuple[str, ...]] = {
    "US": ("in the US", "US filings", "at the USPTO"),
    "EP": ("at the EPO", "European Patent Office filings"),
    "JP": ("Japanese filings", "at the JPO"),
    "CN": ("Chinese filings", "at CNIPA"),
    "WO": ("PCT applications", "WO publications"),
}


TECHS: tuple[Tech, ...] = (
    Tech(
        key="sulfide_sse",
        field_phrase="sulfide solid electrolytes",
        colloquial="solid-state battery electrolytes",
        cpc=("H01M10/0562", "H01M10/052", "H01M2300/0068"),
        terms=(
            "argyrodite",
            "Li6PS5Cl",
            "sulfide solid electrolyte",
            "solid-state cell",
            "lithium superionic conductor",
        ),
        claim_subject="an all-solid-state lithium secondary battery",
        claim_feature=(
            "a solid electrolyte layer comprising an argyrodite-type "
            "Li6PS5Cl having a median particle diameter D50 of 0.8 um to 2.5 um"
        ),
        claim_refinement=(
            "wherein the solid electrolyte layer further comprises 0.5 wt% to "
            "3 wt% of a lithium halide interfacial additive"
        ),
        metrics=(
            Metric("ionic conductivity", "3.2 mS/cm", "at 25 degrees C"),
            Metric("critical current density", "1.8 mA/cm2", "at 60 degrees C"),
            Metric("interfacial resistance", "12 ohm cm2", "after 50 cycles"),
        ),
        problem=(
            "lithium dendrite penetration through the electrolyte at practical "
            "current densities"
        ),
        assignees=("toyota", "samsung_sdi", "quantumscape", "helion"),
    ),
    Tech(
        key="si_anode",
        field_phrase="silicon-composite anodes",
        colloquial="silicon anodes for lithium-ion cells",
        cpc=("H01M4/386", "H01M4/134", "H01M4/62"),
        terms=(
            "silicon-carbon composite anode",
            "SiOx anode",
            "volume expansion buffer",
            "prelithiation",
            "nano-silicon",
        ),
        claim_subject="a negative electrode for a lithium secondary battery",
        claim_feature=(
            "a silicon-carbon composite in which silicon domains of 5 nm to "
            "30 nm are dispersed within a porous carbon scaffold"
        ),
        claim_refinement=(
            "wherein the porous carbon scaffold has a BET specific surface "
            "area of 8 m2/g to 40 m2/g"
        ),
        metrics=(
            Metric("first-cycle coulombic efficiency", "91.4%"),
            Metric("specific capacity", "1,450 mAh/g", "at 0.1C"),
            Metric("capacity retention", "84%", "after 500 cycles"),
        ),
        problem=(
            "capacity fade caused by the ~300% volume expansion of silicon on "
            "lithiation"
        ),
        assignees=("lges", "panasonic", "catl", "cathodica"),
    ),
    Tech(
        key="lfp_cathode",
        field_phrase="lithium iron phosphate cathodes",
        colloquial="LFP cathode materials",
        cpc=("H01M4/5825", "H01M4/136", "C01B25/45"),
        terms=(
            "LiFePO4",
            "olivine cathode",
            "carbon-coated LFP",
            "manganese-doped LFP",
            "LMFP",
        ),
        claim_subject="a positive electrode active material",
        claim_feature=(
            "carbon-coated LiFe(1-x)MnxPO4 particles wherein 0.15 <= x <= 0.45 "
            "and the carbon coating has a thickness of 2 nm to 6 nm"
        ),
        claim_refinement=(
            "wherein the particles have a tap density of at least 1.4 g/cm3"
        ),
        metrics=(
            Metric("discharge capacity", "158 mAh/g", "at 1C and 25 degrees C"),
            Metric("tap density", "1.52 g/cm3"),
            Metric("energy density", "205 Wh/kg", "at cell level"),
        ),
        problem="the low intrinsic electronic conductivity of olivine phosphates",
        assignees=("catl", "basf", "cathodica"),
    ),
    Tech(
        key="perovskite_pv",
        field_phrase="perovskite-silicon tandem photovoltaics",
        colloquial="perovskite tandem solar cells",
        cpc=("H10K30/50", "H10K85/50", "H01L31/0725"),
        terms=(
            "perovskite-silicon tandem",
            "wide-bandgap perovskite",
            "passivating contact",
            "self-assembled monolayer hole transport",
            "2PACz",
        ),
        claim_subject="a monolithic tandem photovoltaic device",
        claim_feature=(
            "a wide-bandgap perovskite top cell having a bandgap of 1.66 eV to "
            "1.72 eV deposited on a textured silicon bottom cell"
        ),
        claim_refinement=(
            "wherein a self-assembled monolayer hole-selective contact is "
            "disposed between the perovskite absorber and the recombination layer"
        ),
        metrics=(
            Metric("power conversion efficiency", "31.2%", "under AM1.5G"),
            Metric("open-circuit voltage", "1.92 V"),
            Metric("efficiency retention", "95%", "after 1,000 h at 85 degrees C / 85% RH"),
        ),
        problem=(
            "moisture- and thermally-driven degradation of the perovskite "
            "absorber under damp-heat testing"
        ),
        assignees=("amat", "basf", "lumenex"),
    ),
    Tech(
        key="base_editing",
        field_phrase="CRISPR base editing",
        colloquial="base editors for gene therapy",
        cpc=("C12N15/11", "C12N9/22", "C12N15/907"),
        terms=(
            "adenine base editor",
            "cytosine base editor",
            "Cas9 nickase",
            "deaminase fusion",
            "guide RNA scaffold",
        ),
        claim_subject="a fusion protein for programmable base editing",
        claim_feature=(
            "a Cas9 nickase domain fused to an engineered TadA-8e adenosine "
            "deaminase domain via a 32-amino-acid linker"
        ),
        claim_refinement=(
            "wherein the fusion protein further comprises two bipartite "
            "nuclear localisation signals"
        ),
        metrics=(
            Metric("on-target editing efficiency", "68%", "in primary human hepatocytes"),
            Metric("bystander edit rate", "under 2%"),
            Metric("indel frequency", "0.4%"),
        ),
        problem="off-target deamination at genomic sites resembling the protospacer",
        assignees=("broad", "genentech", "novastrand"),
    ),
    Tech(
        key="lnp_delivery",
        field_phrase="ionisable lipid nanoparticle delivery",
        colloquial="mRNA lipid nanoparticle formulations",
        cpc=("A61K9/127", "A61K47/54", "C12N15/88"),
        terms=(
            "ionisable cationic lipid",
            "lipid nanoparticle",
            "apparent pKa",
            "PEG-lipid",
            "endosomal escape",
        ),
        claim_subject="a lipid nanoparticle composition encapsulating a messenger RNA",
        claim_feature=(
            "an ionisable cationic lipid having an apparent pKa of 6.2 to 6.7 "
            "present at 45 mol% to 50 mol% of total lipid"
        ),
        claim_refinement=(
            "wherein the PEG-lipid is present at 1.0 mol% to 1.8 mol% of total lipid"
        ),
        metrics=(
            Metric("encapsulation efficiency", "94%"),
            Metric("Z-average particle size", "78 nm", "by dynamic light scattering"),
            Metric("polydispersity index", "0.08"),
        ),
        problem="poor endosomal escape limiting the delivered dose of intact mRNA",
        assignees=("genentech", "novastrand", "broad"),
    ),
    Tech(
        key="mmwave_beamforming",
        field_phrase="millimetre-wave hybrid beamforming",
        colloquial="mmWave beamforming for 5G base stations",
        cpc=("H04B7/0456", "H04B7/06", "H04W16/28"),
        terms=(
            "hybrid analog-digital beamforming",
            "precoding matrix indicator",
            "beam sweep",
            "codebook subset restriction",
            "massive MIMO",
        ),
        claim_subject="a method of beam management in a wireless network",
        claim_feature=(
            "selecting an analog beam from a hierarchical codebook based on a "
            "reported layer-1 reference signal received power for each of at "
            "least four candidate beams"
        ),
        claim_refinement=(
            "wherein the beam sweep periodicity is adapted according to a "
            "measured Doppler spread of the channel"
        ),
        metrics=(
            Metric("beam-selection latency", "3.5 ms"),
            Metric("spectral efficiency gain", "22%", "over exhaustive sweep"),
            Metric("reference signal overhead", "1.8% of resource elements"),
        ),
        problem=(
            "the pilot overhead and latency of exhaustive beam sweeping in "
            "large antenna arrays"
        ),
        assignees=("ericsson", "huawei", "qualcomm", "beamforge"),
    ),
    Tech(
        key="federated_learning",
        field_phrase="privacy-preserving federated learning",
        colloquial="federated learning with differential privacy",
        cpc=("G06N3/098", "G06N3/045", "H04L9/008"),
        terms=(
            "federated averaging",
            "secure aggregation",
            "differential privacy budget",
            "client drift",
            "gradient clipping",
        ),
        claim_subject="a method of training a machine-learning model across client devices",
        claim_feature=(
            "applying per-client gradient clipping to a fixed L2 norm before "
            "adding Gaussian noise calibrated to a per-round privacy budget"
        ),
        claim_refinement=(
            "wherein client updates are combined under a secure aggregation "
            "protocol such that the server observes only the sum"
        ),
        metrics=(
            Metric("privacy budget", "epsilon = 2.1", "at delta = 1e-5"),
            Metric("accuracy drop", "1.9 percentage points", "versus centralised training"),
            Metric("communication rounds", "180"),
        ),
        problem=(
            "the accuracy cost of noise addition when client data are "
            "non-identically distributed"
        ),
        assignees=("qualcomm", "huawei", "philips"),
    ),
    Tech(
        key="fmcw_lidar",
        field_phrase="frequency-modulated continuous-wave LiDAR",
        colloquial="FMCW LiDAR for automotive sensing",
        cpc=("G01S17/34", "G01S7/4911", "G02F1/295"),
        terms=(
            "FMCW LiDAR",
            "coherent detection",
            "optical phased array",
            "chirp linearisation",
            "instantaneous velocity measurement",
        ),
        claim_subject="a coherent optical ranging system",
        claim_feature=(
            "a silicon-photonic optical phased array configured to steer a "
            "frequency-chirped beam without a moving part, and a balanced "
            "photodetector pair for coherent detection"
        ),
        claim_refinement=(
            "wherein a chirp non-linearity is corrected using a reference "
            "interferometer having a fixed delay"
        ),
        metrics=(
            Metric("range", "260 m", "at 10% target reflectivity"),
            Metric("velocity precision", "0.05 m/s"),
            Metric("angular resolution", "0.05 degrees"),
        ),
        problem=(
            "the cost and reliability penalty of mechanically scanned "
            "time-of-flight LiDAR in automotive service"
        ),
        assignees=("bosch", "lumenex", "beamforge"),
    ),
    Tech(
        key="pem_electrolyser",
        field_phrase="proton-exchange-membrane water electrolysis",
        colloquial="PEM electrolysers for green hydrogen",
        cpc=("C25B1/04", "C25B11/081", "C25B9/23"),
        terms=(
            "PEM electrolyser",
            "iridium oxide anode catalyst",
            "membrane electrode assembly",
            "catalyst coated membrane",
            "iridium thrifting",
        ),
        claim_subject="a membrane electrode assembly for water electrolysis",
        claim_feature=(
            "an anode catalyst layer having an iridium loading of 0.15 mg/cm2 "
            "to 0.35 mg/cm2 supported on antimony-doped tin oxide"
        ),
        claim_refinement=(
            "wherein the proton-exchange membrane has a thickness of 50 um to "
            "90 um and a reinforcing expanded-PTFE layer"
        ),
        metrics=(
            Metric("cell voltage", "1.72 V", "at 2 A/cm2"),
            Metric("degradation rate", "8 uV/h", "over 4,000 h"),
            Metric("iridium loading", "0.22 mg/cm2"),
        ),
        problem=(
            "the scarcity of iridium at the anode limiting terawatt-scale "
            "electrolyser deployment"
        ),
        assignees=("siemens", "topsoe", "basf", "helion"),
    ),
    Tech(
        key="microled_transfer",
        field_phrase="microLED mass-transfer processes",
        colloquial="microLED display mass transfer",
        cpc=("H01L25/0753", "H01L33/62", "H01L21/6835"),
        terms=(
            "mass transfer",
            "elastomeric stamp transfer",
            "laser lift-off",
            "microLED die",
            "transfer yield",
        ),
        claim_subject="a method of transferring micro light-emitting diodes to a backplane",
        claim_feature=(
            "picking up an array of micro light-emitting diodes having a "
            "lateral dimension of less than 30 um with an elastomeric stamp "
            "whose adhesion is modulated by peel rate"
        ),
        claim_refinement=(
            "wherein a laser lift-off step releases the diodes from a sapphire "
            "growth substrate before pick-up"
        ),
        metrics=(
            Metric("transfer yield", "99.994%"),
            Metric("placement accuracy", "+/- 1.2 um", "3 sigma"),
            Metric("throughput", "42 million dies per hour"),
        ),
        problem=(
            "the defect rate of transferring millions of dies per panel at "
            "commercially viable throughput"
        ),
        assignees=("amat", "hanwoo", "lumenex", "beamforge"),
    ),
    Tech(
        key="amine_capture",
        field_phrase="amine-based post-combustion carbon capture",
        colloquial="amine solvent CO2 capture",
        cpc=("B01D53/1475", "B01D53/62", "B01D2252/20421"),
        terms=(
            "post-combustion capture",
            "sterically hindered amine",
            "reboiler duty",
            "solvent degradation",
            "lean loading",
        ),
        claim_subject="a process for removing carbon dioxide from a flue gas",
        claim_feature=(
            "contacting the flue gas with an aqueous solvent comprising a "
            "sterically hindered amine and a piperazine promoter at a total "
            "amine concentration of 30 wt% to 45 wt%"
        ),
        claim_refinement=(
            "wherein the regeneration is carried out at a reboiler temperature "
            "of 115 degrees C to 125 degrees C"
        ),
        metrics=(
            Metric("capture rate", "95.3%"),
            Metric("reboiler duty", "2.4 GJ per tonne CO2"),
            Metric("solvent degradation rate", "0.31 kg per tonne CO2"),
        ),
        problem=(
            "the steam consumption of solvent regeneration, which dominates "
            "the operating cost of capture"
        ),
        assignees=("basf", "topsoe", "siemens", "aerio"),
    ),
    Tech(
        key="heat_pump",
        field_phrase="low-GWP refrigerant heat pumps",
        colloquial="propane heat pumps for domestic heating",
        cpc=("F25B30/02", "F25B13/00", "C09K5/044"),
        terms=(
            "low-GWP refrigerant",
            "R290 propane",
            "vapour injection",
            "coefficient of performance",
            "charge minimisation",
        ),
        claim_subject="a vapour-compression heat pump",
        claim_feature=(
            "a propane refrigerant circuit having a total charge of less than "
            "150 g and a microchannel evaporator with an internal volume of "
            "less than 0.4 L"
        ),
        claim_refinement=(
            "wherein an economiser provides vapour injection to the compressor "
            "at an intermediate pressure"
        ),
        metrics=(
            Metric("coefficient of performance", "4.1", "at A7/W35"),
            Metric("refrigerant charge", "128 g"),
            Metric("sound power level", "48 dB(A)"),
        ),
        problem=(
            "the flammability charge limits that restrict hydrocarbon "
            "refrigerants in domestic installations"
        ),
        assignees=("bosch", "philips", "aerio", "siemens"),
    ),
    Tech(
        key="qec_surface_code",
        field_phrase="surface-code quantum error correction",
        colloquial="quantum error correction decoders",
        cpc=("G06N10/70", "G06N10/40", "H03M13/1102"),
        terms=(
            "surface code",
            "syndrome decoding",
            "minimum-weight perfect matching",
            "logical error rate",
            "code distance",
        ),
        claim_subject="a control system for a quantum processor",
        claim_feature=(
            "a real-time syndrome decoder implemented in field-programmable "
            "logic and configured to return a correction within one surface-code "
            "cycle for a code distance of at least 7"
        ),
        claim_refinement=(
            "wherein the decoder applies a correlated-noise weighting derived "
            "from a calibration of two-qubit gate errors"
        ),
        metrics=(
            Metric("logical error rate per cycle", "1.4e-4", "at distance 7"),
            Metric("decoder latency", "620 ns"),
            Metric("syndrome cycle time", "1.1 us"),
        ),
        problem=(
            "decoder throughput falling behind the syndrome extraction rate, "
            "which causes a backlog and destroys the logical qubit"
        ),
        assignees=("veriqubit", "siemens", "beamforge"),
    ),
)


def tech_by_key(key: str) -> Tech:
    for tech in TECHS:
        if tech.key == key:
            return tech
    raise KeyError(f"unknown tech {key!r}; known: {[t.key for t in TECHS]}")
