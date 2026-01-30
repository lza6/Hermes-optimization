import re
from typing import List, Set, Dict

PREFIX_REGEX = re.compile(r"^(models|model|m)/", re.IGNORECASE)
SEPARATOR_REGEX = re.compile(r"[-_\s:]+")
VARIANT_TOKENS = {
    "latest", "default", "stable", "fast", "turbo", "slow", "high", "low",
    "medium", "mini", "lite", "light", "pro", "ultra", "think", "thinking",
    "instruct", "chat", "online", "beta", "preview", "docs", "free", "max", "xhigh"
}

class NormalizedModel:
    def __init__(self, raw, cleaned, canonical, family_key, version_parts):
        self.raw = raw
        self.cleaned = cleaned
        self.canonical = canonical
        self.family_key = family_key
        self.version_parts = version_parts

def parse_version(token: str) -> List[int]:
    match = re.match(r"^v?(\d+(?:\.\d+)+|\d+)$", token)
    if not match: return None
    return [int(n) for n in match.group(1).split(".")]

def compare_version_parts(a: List[int], b: List[int]) -> int:
    max_len = max(len(a), len(b))
    for i in range(max_len):
        av = a[i] if i < len(a) else 0
        bv = b[i] if i < len(b) else 0
        if av != bv:
            return av - bv
    return 0

def normalize_model_name(raw: str) -> NormalizedModel:
    cleaned = PREFIX_REGEX.sub("", raw.strip()).lower()
    
    without_vendor = cleaned
    if "/" in cleaned:
        without_vendor = cleaned.split("/")[-1] or cleaned
    
    tokens = [t for t in SEPARATOR_REGEX.split(without_vendor) if t]
    
    version_tokens = []
    canonical_tokens = []
    family_tokens = []
    
    for token in tokens:
        # Long numeric tag check
        if re.match(r"^\d{4,}$", token):
            continue
            
        version = parse_version(token)
        if version:
            version_tokens.append(version)
            canonical_tokens.append(token)
            continue
            
        if token in VARIANT_TOKENS:
            continue
            
        canonical_tokens.append(token)
        family_tokens.append(token)
        
    version_parts = version_tokens[0] if version_tokens else []
    
    canonical_base = "-".join(canonical_tokens)
    family_base = "-".join(family_tokens)
    
    canonical = canonical_base or without_vendor
    family_key = family_base or without_vendor
    
    return NormalizedModel(raw, cleaned, canonical, family_key, version_parts)


class ModelAliasMaps:
    def __init__(self, canonical_to_variants, variant_to_canonical):
        self.canonical_to_variants = canonical_to_variants
        self.variant_to_canonical = variant_to_canonical

def build_model_alias_maps(models_by_provider: List[Dict]) -> ModelAliasMaps:
    # models_by_provider: list of dicts with 'models' key (List[str])
    
    family_map = {} # familyKey -> {variants: Set, candidates: List}
    
    for provider in models_by_provider:
        for raw_model in provider.get("models", []):
            info = normalize_model_name(raw_model)
            family_key = info.family_key or info.canonical or info.cleaned
            
            if family_key not in family_map:
                family_map[family_key] = {"variants": set(), "candidates": []}
            
            entry = family_map[family_key]
            entry["variants"].add(raw_model)
            entry["candidates"].append({"canonical": info.canonical or info.cleaned, "version": info.version_parts})
            
    canonical_to_variants = {}
    variant_to_canonical = {}
    
    for _, entry in family_map.items():
        with_version = [c for c in entry["candidates"] if c["version"]]
        preferred = entry["candidates"][0]
        
        if with_version:
            # sort desc
            with_version.sort(key=lambda x: x["version"], reverse=True) # Python's list comparison works lexicographically usually for lists, but custom comparator logic:
            # Actually standard list comparison is fine for [1,0] > [0,5]
            preferred = with_version[0]
            
        variant_set = entry["variants"]
        canonical_to_variants[preferred["canonical"]] = variant_set
        
        for variant in variant_set:
            norm = normalize_model_name(variant).canonical
            variant_to_canonical[norm] = preferred["canonical"]
            variant_to_canonical[variant] = preferred["canonical"]
            
        preferred_norm = normalize_model_name(preferred["canonical"]).canonical
        variant_to_canonical[preferred["canonical"]] = preferred["canonical"]
        variant_to_canonical[preferred_norm] = preferred["canonical"]
        
    return ModelAliasMaps(canonical_to_variants, variant_to_canonical)
