"""Provider-independent product contract: WHAT scientific imagery
DataLoader promises downstream, kept separate from WHERE it comes from
(the provider) and from DataLoader's own processing choices (target CRS/
resolution, QA policy, indices, reducer -- see engine.py/manifest.py).

Deliberately small: this only captures upstream product *identity* (sensor,
product family, native temporal structure) and, for a request, how to
resolve multiple upstream processing versions of one acquisition if the
provider can return them. It says nothing about QA masking, target grid,
indices, or compositing -- those are request/output provenance, not
upstream product identity, precisely because two providers can serve the
identical scientific product while DataLoader's own processing of it
differs run to run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Upstream product families currently understood. Deliberately not a class
# hierarchy or plugin registry -- four fixed strings is the whole alphabet
# this version needs, and a typo should fail loudly (see ProductIdentity's
# validation) rather than silently create a new "family".
USGS_C2_L2 = "usgs_c2_l2"
ESA_S2_L2A = "esa_s2_l2a"
# Google's COPERNICUS/S2_SR_HARMONIZED -- ESA L2A with Google's own
# baseline-offset harmonization applied. Deliberately NOT the same family
# as ESA_S2_L2A: it is not a byte-for-byte passthrough of what PC/Earth
# Search serve, so treating it as interchangeable with raw ESA L2A would
# repeat the exact silent-substitution mistake this system exists to catch.
ESA_S2_L2A_HARMONIZED = "esa_s2_l2a_harmonized"
GLAD_ARD = "glad_ard"
# USGS Landsat Collection 2 U.S. Analysis Ready Data, Surface Reflectance --
# deliberately NOT the same family as USGS_C2_L2. Same underlying Level-1
# archive and SR processing chain, but ARD reprojects/tiles onto a fixed
# national Albers Equal-Area grid (5000x5000px, 30m tiles) rather than
# per-scene UTM footprints, and one ARD tile item can mosaic more than one
# adjacent WRS-2 scene from the same overpass (`landsat:scene_count`) --
# a real structural difference from a single scene, not just a different
# provider serving the same product. See providers/usgs_ard.py.
USGS_ARD_SR = "usgs_ard_sr"

PRODUCT_FAMILIES = frozenset({USGS_C2_L2, ESA_S2_L2A, ESA_S2_L2A_HARMONIZED, GLAD_ARD, USGS_ARD_SR})

SCENE = "scene"
FIXED_COMPOSITE = "fixed_composite"

TEMPORAL_PRODUCTS = frozenset({SCENE, FIXED_COMPOSITE})

_VERSION_POLICY_SIMPLE = frozenset({"any", "latest", "allow_mixed"})
_PINNED_RE = re.compile(r"^pinned:(.+)$")


def parse_version_policy(policy: str) -> tuple[str, Optional[str]]:
    """"any" | "latest" | "allow_mixed" | "pinned:<baseline>" -> (kind, baseline_or_None).

    Raises ValueError on anything else -- a typo'd policy should fail
    loudly at config-load time, not be silently treated as "any"."""
    if policy in _VERSION_POLICY_SIMPLE:
        return policy, None
    m = _PINNED_RE.match(policy)
    if m and m.group(1):
        return "pinned", m.group(1)
    raise ValueError(
        f"Unknown processing_version_policy {policy!r} -- expected one of "
        f"'any', 'latest', 'allow_mixed', or 'pinned:<baseline>' (e.g. "
        f"'pinned:05.00')."
    )


@dataclass(frozen=True)
class ProductIdentity:
    """Static WHAT a provider supplies for one sensor -- independent of any
    particular request. This is what a provider declares via
    `Provider.capabilities()`. Two providers whose ProductIdentity for a
    sensor is equal are candidates for being treated as the same scientific
    product; if it differs, they are not, regardless of what the config's
    `provider:` string or `sensor:` name might suggest."""

    sensor: str
    product_family: str
    temporal_product: str

    def __post_init__(self):
        if self.product_family not in PRODUCT_FAMILIES:
            raise ValueError(
                f"Unknown product_family {self.product_family!r} -- expected "
                f"one of {sorted(PRODUCT_FAMILIES)}"
            )
        if self.temporal_product not in TEMPORAL_PRODUCTS:
            raise ValueError(
                f"Unknown temporal_product {self.temporal_product!r} -- "
                f"expected one of {sorted(TEMPORAL_PRODUCTS)}"
            )


@dataclass(frozen=True)
class ProductContract(ProductIdentity):
    """What a request asks for: a ProductIdentity plus how to resolve
    multiple upstream processing versions of one acquisition, if the
    provider can return them (see select_processing_versions). A provider
    is eligible for a request only if its declared ProductIdentity matches
    (sensor, product_family, temporal_product) -- see resolve_contract."""

    processing_version_policy: str = "any"

    def __post_init__(self):
        super().__post_init__()
        parse_version_policy(self.processing_version_policy)


def resolve_contract(
    sensor: str,
    identity: ProductIdentity,
    requested_product_family: Optional[str],
    requested_policy: str,
) -> ProductContract:
    """Resolve a sensor's request against what its provider actually
    supplies. `requested_product_family=None` means "no explicit request
    was made -- accept whatever this provider naturally supplies" (the
    backward-compatible default for existing configs). An explicit,
    mismatched request raises: substituting one product family for another
    is exactly the silent-mixing failure mode this module exists to
    prevent, so it is never done quietly."""
    if identity.sensor != sensor:
        raise ValueError(
            f"internal error: capability lookup for sensor {sensor!r} "
            f"returned an identity for sensor {identity.sensor!r}"
        )
    resolved_family = requested_product_family or identity.product_family
    if resolved_family != identity.product_family:
        raise ValueError(
            f"requested product_family {resolved_family!r} for sensor "
            f"{sensor!r}, but this provider supplies {identity.product_family!r} "
            f"for that sensor -- these are not the same scientific product "
            f"and cannot be substituted for each other."
        )
    return ProductContract(
        sensor=sensor,
        product_family=resolved_family,
        temporal_product=identity.temporal_product,
        processing_version_policy=requested_policy,
    )


@dataclass(frozen=True)
class AcquisitionProvenance:
    """Per-scene provenance: what upstream product a given SceneRef
    actually is, recorded so a run can be reproduced/audited later. Fields
    a provider genuinely cannot supply stay None -- this is intentionally
    not a strict schema every provider must fully populate."""

    provider: str
    provider_item_id: str
    upstream_product_id: Optional[str] = None
    acquisition_datetime: Optional[str] = None  # full ISO8601 timestamp, not just a date
    platform: Optional[str] = None
    product_family: Optional[str] = None
    collection: Optional[str] = None
    processing_baseline: Optional[str] = None
    generation_time: Optional[str] = None
    extra: dict = field(default_factory=dict)
    # Path (relative to the run's output directory) to a full snapshot of
    # this provider's own source metadata record for this exact selected
    # product/version -- see data_loader.metadata_snapshot. None if the
    # run didn't write files (no output directory to put it in) or the
    # provider couldn't supply one.
    source_metadata_ref: Optional[str] = None


@dataclass(frozen=True)
class VersionCandidate:
    """One provider item as input to select_processing_versions -- opaque
    enough to work for any provider's native item type via `payload`."""

    key: tuple  # stable acquisition identity, e.g. (datatake_id_core, mgrs_tile)
    baseline: Optional[str]
    generation_time: Optional[str]  # ISO8601 string; lexicographically sortable
    payload: object


def _rank(c: VersionCandidate) -> tuple[str, str]:
    return (c.baseline or "", c.generation_time or "")


def select_processing_versions(candidates: list[VersionCandidate], policy: str) -> list[object]:
    """Group candidates by stable acquisition identity (`key`) and pick (at
    most) one per group per `policy`:

    - "allow_mixed": no deduplication -- every candidate is returned. The
      explicit opt-in for exploratory/research work; never the default.
    - "pinned:<baseline>": keep only items matching that exact baseline
      within each group. A group with no match at that baseline is simply
      not represented in the result (that acquisition isn't available at
      the pinned baseline) -- not an error by itself.
    - "any" / "latest": within each group, pick the highest baseline,
      breaking ties by the latest generation_time.

    Raises RuntimeError if a group still has more than one candidate after
    the above (a genuine, unresolvable tie) rather than picking one
    arbitrarily -- mixed-version behavior must never be the silent
    default outcome of an ambiguous case.
    """
    kind, pinned_baseline = parse_version_policy(policy)

    groups: dict[tuple, list[VersionCandidate]] = {}
    for c in candidates:
        groups.setdefault(c.key, []).append(c)

    selected: list[object] = []
    for key, group in groups.items():
        if kind == "allow_mixed":
            selected.extend(c.payload for c in group)
            continue

        if kind == "pinned":
            group = [c for c in group if c.baseline == pinned_baseline]
            if not group:
                continue  # acquisition not available at the pinned baseline

        if len(group) > 1:
            best = max(_rank(c) for c in group)
            group = [c for c in group if _rank(c) == best]

        if len(group) != 1:
            raise RuntimeError(
                f"[product_contract] cannot resolve a single processing "
                f"version for acquisition {key!r} under policy {policy!r} -- "
                f"{len(group)} equally-ranked candidates remain "
                f"(baselines={[c.baseline for c in group]}, "
                f"generation_times={[c.generation_time for c in group]}). "
                f"Pass processing_version_policy: allow_mixed if returning "
                f"all of them is genuinely intended."
            )
        selected.append(group[0].payload)

    return selected
