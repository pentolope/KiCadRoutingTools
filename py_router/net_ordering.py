"""
Net ordering strategies for PCB routing.

This module provides different strategies for ordering nets before routing,
including MPS (Maximum Planar Subset), inside-out BGA ordering, and original order.
"""
from __future__ import annotations

from typing import List, Tuple, Dict, Optional, Set

from kicad_parser import PCBData
from routing_config import DiffPairNet


def order_nets_mps(
    pcb_data: PCBData,
    net_ids: List[Tuple[str, int]],
    diff_pairs: Dict[str, DiffPairNet],
    mps_unroll: bool,
    bga_exclusion_zones: Optional[List[Tuple[float, float, float, float]]],
    mps_reverse_rounds: int,
    crossing_layer_check: bool,
    mps_segment_intersection: bool,
    mps_layer_swap: bool = False,
    enable_layer_switch: bool = False,
    config=None,
    can_swap_to_top_layer: bool = True,
    all_segment_modifications: List = None,
    all_swap_vias: List = None,
    all_stubs_by_layer: Dict = None,
    stub_endpoints_by_layer: Dict = None,
    verbose: bool = False
) -> Tuple[List[Tuple[str, int]], int]:
    """
    Order nets using Maximum Planar Subset algorithm.

    Args:
        pcb_data: PCB data structure
        net_ids: List of (name, id) tuples for nets to order
        diff_pairs: Dictionary of detected diff pairs
        mps_unroll: Whether to use MPS unroll boundary ordering
        bga_exclusion_zones: BGA zones for ordering
        mps_reverse_rounds: Number of MPS reverse rounds
        crossing_layer_check: Whether to check crossing layers
        mps_segment_intersection: Use segment intersection for MPS
        mps_layer_swap: Whether to use MPS-aware layer swaps
        enable_layer_switch: Whether layer switching is enabled
        config: Routing configuration (for layer swaps)
        can_swap_to_top_layer: Whether top layer swaps are allowed
        all_segment_modifications: List to append segment mods (for layer swaps)
        all_swap_vias: List to append swap vias (for layer swaps)
        all_stubs_by_layer: Stubs by layer (for layer swaps)
        stub_endpoints_by_layer: Stub endpoints by layer (for layer swaps)
        verbose: Print detailed output

    Returns:
        Tuple of (ordered_net_ids, num_layer_swaps)
    """
    from net_queries import compute_mps_net_ordering

    print("\nUsing MPS ordering strategy...")
    all_net_ids = [nid for _, nid in net_ids]
    total_layer_swaps = 0

    # If MPS layer swap is enabled, get extended info for conflict analysis
    if mps_layer_swap and enable_layer_switch and config is not None:
        from mps_layer_swap import try_mps_aware_layer_swaps

        mps_result = compute_mps_net_ordering(
            pcb_data, all_net_ids, diff_pairs=diff_pairs,
            use_boundary_ordering=mps_unroll,
            bga_exclusion_zones=bga_exclusion_zones,
            reverse_rounds=mps_reverse_rounds,
            crossing_layer_check=crossing_layer_check,
            return_extended_info=True,
            use_segment_intersection=True if mps_segment_intersection else None
        )

        if mps_result.num_rounds > 1:
            print(f"\nMPS detected {mps_result.num_rounds} rounds - attempting layer swaps to reduce crossings...")

            swap_result = try_mps_aware_layer_swaps(
                pcb_data, config, mps_result, diff_pairs,
                available_layers=config.routable_layers(),
                can_swap_to_top_layer=can_swap_to_top_layer,
                all_segment_modifications=all_segment_modifications,
                all_swap_vias=all_swap_vias,
                all_stubs_by_layer=all_stubs_by_layer,
                stub_endpoints_by_layer=stub_endpoints_by_layer,
                verbose=verbose
            )

            if swap_result.swaps_applied > 0:
                total_layer_swaps = swap_result.swaps_applied
                # Re-run MPS ordering with updated layer assignments
                print("Re-running MPS ordering after layer swaps...")
                ordered_ids = compute_mps_net_ordering(
                    pcb_data, all_net_ids, diff_pairs=diff_pairs,
                    use_boundary_ordering=mps_unroll,
                    bga_exclusion_zones=bga_exclusion_zones,
                    reverse_rounds=mps_reverse_rounds,
                    crossing_layer_check=crossing_layer_check,
                    use_segment_intersection=True if mps_segment_intersection else None
                )
            else:
                ordered_ids = mps_result.ordered_ids
        else:
            ordered_ids = mps_result.ordered_ids
    else:
        ordered_ids = compute_mps_net_ordering(
            pcb_data, all_net_ids, diff_pairs=diff_pairs,
            use_boundary_ordering=mps_unroll,
            bga_exclusion_zones=bga_exclusion_zones,
            reverse_rounds=mps_reverse_rounds,
            crossing_layer_check=crossing_layer_check,
            use_segment_intersection=True if mps_segment_intersection else None
        )

    # Rebuild net_ids in the new order
    id_to_name = {nid: name for name, nid in net_ids}
    ordered_net_ids = [(id_to_name[nid], nid) for nid in ordered_ids if nid in id_to_name]

    return ordered_net_ids, total_layer_swaps


def order_pin_joins_last(net_ids: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
    """Move the 'unconnected-*' nets to the end, keeping both partitions' order (#1405).

    Such a net joins one footprint's same-number pads (a reversible
    footprint's doubled pins, a connector's shield tabs), which no signal
    depends on. Routed early, it claims corridors the named nets need; routed
    last it gets what a separate final step used to give it.
    """
    pin_join = [t for t in net_ids if (t[0] or '').lower().startswith('unconnected-')]
    if not pin_join or len(pin_join) == len(net_ids):
        return net_ids
    print(f"Pin-join nets last (#1405): {len(pin_join)} 'unconnected-*' "
          f"net(s) routed after the named nets")
    return [t for t in net_ids if t not in pin_join] + pin_join


#: A route-first pattern with one of these is a wildcard: it may select nets
#: outside the run, and only the ones inside count.
_WILDCARD_CHARS = '*?['


def resolve_route_first(pcb_data: PCBData, patterns: List[str],
                        scope: Optional[List[str]] = None) -> List[str]:
    """The nets a route-first list names, in the order it names them.

    Each pattern is read as --nets reads it (expand_net_patterns: wildcards,
    a sheet's last component); one pattern's nets come in name order, and a
    net two patterns select comes once, at its first. With SCOPE (the run's
    nets) a wildcard keeps the nets it selects inside SCOPE, and a name
    selects only itself. Raises ValueError, with the reason, for a pattern
    that selects no net, an exclusion (a leading '!' that names no net), a
    wildcard that selects none of SCOPE and a name outside SCOPE: a priority
    that cannot be honoured is refused, not dropped.
    """
    from net_queries import expand_net_patterns
    names = {n.name for n in pcb_data.nets.values() if n.name}
    inside = None if scope is None else set(scope)
    first: List[str] = []
    outside: List[str] = []
    for p in patterns:
        if p.startswith('!') and p not in names:
            raise ValueError(f"--route-first: {p} is an exclusion; name the nets to route first")
        got = [n for n in expand_net_patterns(pcb_data, [p], quiet=True) if n in names]
        if not got:
            raise ValueError(f"--route-first: {p} selects no net on this board")
        if inside is not None:
            if any(c in p for c in _WILDCARD_CHARS):
                got = [n for n in got if n in inside]
                if not got:
                    raise ValueError(f"--route-first: {p} selects none of the nets "
                                     f"this run routes")
            else:
                outside += [n for n in got if n not in inside]
        first += [n for n in got if n not in first]
    if outside:
        raise ValueError(f"--route-first: {', '.join(outside)} not among the nets "
                         f"this run routes")
    return first


def order_route_first(items: list, first: List[str], name=lambda t: t[0]) -> list:
    """Move the ITEMS whose NAME is one of FIRST to the front, in FIRST's
    order; every other item keeps its place relative to the rest, so the
    order it came in (mps, bus groups, direct-first, the global plan, Phase
    3's boxed-in risk) stands for them. A FIRST net not in ITEMS (already
    connected) is skipped."""
    rank = {n: i for i, n in enumerate(first)}
    front = sorted((t for t in items if name(t) in rank), key=lambda t: rank[name(t)])
    if not front:
        return items
    print(f"Route-first ordering: {len(front)} declared net(s) routed first: "
          f"{', '.join(name(t) for t in front[:8])}{', ...' if len(front) > 8 else ''}")
    return front + [t for t in items if name(t) not in rank]


def route_first_record(first: List[str], main_order: List[str],
                       phase3_order: List[str]) -> Dict:
    """The JSON summary's `route_first`: the declared nets (FIRST), those the
    single-ended loop started, in the order it started them (`main_loop`),
    those Phase 3 routed taps for, in its order (`phase3`), and `honoured`:
    in each, the declared nets came before every other net, in FIRST's
    order. None when neither routed a net. Only this run's own loops count;
    a nested sub-run (reconciliation, plane finalize) is not this order."""
    rank = {n: i for i, n in enumerate(first)}

    def seen(seq):
        seq = list(dict.fromkeys(seq))
        got = [n for n in seq if n in rank]
        return got, (seq[:len(got)] == got and got == sorted(got, key=rank.get))

    main, main_ok = seen(main_order)
    p3, p3_ok = seen(phase3_order)
    return {'nets': list(first), 'main_loop': main, 'phase3': p3,
            'honoured': (main_ok and p3_ok) if (main_order or phase3_order) else None}


def order_nets_inside_out(
    pcb_data: PCBData,
    net_ids: List[Tuple[str, int]],
    bga_exclusion_zones: List[Tuple[float, float, float, float]]
) -> List[Tuple[str, int]]:
    """
    Order nets inside-out from BGA center(s) for better escape routing.

    Only reorders nets that have pads inside a BGA zone. Non-BGA nets
    are kept in original order and placed after BGA nets.

    Args:
        pcb_data: PCB data structure
        net_ids: List of (name, id) tuples for nets to order
        bga_exclusion_zones: List of (x1, y1, x2, y2) BGA zone tuples

    Returns:
        Ordered list of (name, id) tuples
    """
    def pad_in_bga_zone(pad):
        """Check if a pad is inside any BGA zone."""
        for zone in bga_exclusion_zones:
            if zone[0] <= pad.global_x <= zone[2] and zone[1] <= pad.global_y <= zone[3]:
                return True
        return False

    def get_min_distance_to_bga_center(net_id):
        """Get minimum distance from any BGA pad of this net to its BGA center."""
        pads = pcb_data.pads_by_net.get(net_id, [])
        if not pads:
            return float('inf')

        min_dist = float('inf')
        for zone in bga_exclusion_zones:
            center_x = (zone[0] + zone[2]) / 2
            center_y = (zone[1] + zone[3]) / 2
            for pad in pads:
                # Only consider pads that are inside this BGA zone
                if zone[0] <= pad.global_x <= zone[2] and zone[1] <= pad.global_y <= zone[3]:
                    dist = ((pad.global_x - center_x) ** 2 + (pad.global_y - center_y) ** 2) ** 0.5
                    min_dist = min(min_dist, dist)
        return min_dist

    # Separate BGA nets from non-BGA nets
    bga_nets = []
    non_bga_nets = []
    for net_name, net_id in net_ids:
        pads = pcb_data.pads_by_net.get(net_id, [])
        has_bga_pad = any(pad_in_bga_zone(pad) for pad in pads)
        if has_bga_pad:
            bga_nets.append((net_name, net_id))
        else:
            non_bga_nets.append((net_name, net_id))

    # Sort BGA nets inside-out, keep non-BGA nets in original order
    bga_nets.sort(key=lambda x: get_min_distance_to_bga_center(x[1]))
    ordered_net_ids = bga_nets + non_bga_nets

    if bga_nets:
        print(f"\nSorted {len(bga_nets)} BGA nets inside-out ({len(non_bga_nets)} non-BGA nets unchanged)")

    return ordered_net_ids


def separate_nets_by_type(
    net_ids: List[Tuple[str, int]],
    diff_pairs: Dict[str, DiffPairNet],
    diff_pair_net_ids: Set[int]
) -> Tuple[List[Tuple[str, DiffPairNet]], List[Tuple[str, int]]]:
    """
    Separate ordered nets into diff pairs and single-ended nets.

    Args:
        net_ids: Ordered list of (name, id) tuples
        diff_pairs: Dictionary of detected diff pairs
        diff_pair_net_ids: Set of net IDs belonging to diff pairs

    Returns:
        Tuple of (diff_pair_ids_to_route, single_ended_nets)
        where diff_pair_ids_to_route is List[(pair_name, DiffPairNet)]
        and single_ended_nets is List[(net_name, net_id)]
    """
    single_ended_nets = []
    diff_pair_ids_to_route = []  # (pair_name, pair) tuples
    processed_pair_net_ids = set()

    for net_name, net_id in net_ids:
        if net_id in diff_pair_net_ids and net_id not in processed_pair_net_ids:
            # Find the differential pair this net belongs to
            for pair_name, pair in diff_pairs.items():
                if pair.p_net_id == net_id or pair.n_net_id == net_id:
                    diff_pair_ids_to_route.append((pair_name, pair))
                    processed_pair_net_ids.add(pair.p_net_id)
                    processed_pair_net_ids.add(pair.n_net_id)
                    break
        elif net_id not in diff_pair_net_ids:
            single_ended_nets.append((net_name, net_id))

    return diff_pair_ids_to_route, single_ended_nets
