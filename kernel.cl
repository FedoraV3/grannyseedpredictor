/*
 * kernel.cl
 *
 * OpenCL GPU port of simulator.py's GeneratePlacement pipeline -- THE FULL
 * RETRY LOOP (attempt = 1..50, `NetRandom(seed + attempt)` re-seeded each
 * attempt, exactly mirroring simulate_verbose's own `while attempt <
 * max_attempts and not success:` loop) -- and net_random.py's NetRandom
 * (legacy .NET System.Random, Knuth subtractive lagged-Fibonacci generator).
 * See re_gpu.md for the full design writeup.
 *
 * ACCEPTANCE TEST (the correctness-critical piece): `DetectCircularDependencies`
 * / `ValidatePuzzleDependencies` are both driven entirely by
 * `GetEffectivePuzzleOfItem`, which only ever returns non-null by finding some
 * "container" item (non-empty containedItems) whose *current slot* is a
 * puzzle spawn (see re_search.md S4.2 for the full proof, ported verbatim
 * here). Consequences, both ported below:
 *
 *   1. PROVABLE FAST PATH (`guaranteed_accept` in `simulate_one_attempt`): if,
 *      right after Step D (the last step that can ever place an item on a
 *      puzzle -- Steps E/F only ever place into FREE spots), no container
 *      item's `result_slot` is a puzzle slot, then acceptance is analytically
 *      guaranteed true for the rest of this attempt no matter what Steps E/F
 *      do -- `DetectCircularDependencies`/`ValidatePuzzleDependencies` never
 *      need to run at all, and PIN-mismatch early-abort is now safe to enable
 *      (`compute_real_acceptance` is skipped entirely on this path).
 *   2. REAL CHECK (`compute_real_acceptance`): otherwise (some container DID
 *      land on a puzzle), the real accept/reject decision is computed for
 *      real -- `resolve_effective_puzzle_bfs` resolves "which puzzle (if any)
 *      produces this item" purely via slot ids (a container's `result_slot`
 *      tells us directly and exactly which puzzle it's sitting at, with no
 *      floating-point spatial distance computation needed -- see re_gpu.md
 *      for why this is exact, not an approximation, given this scene's free
 *      spot positions never coincide with a puzzle spawn position), followed
 *      by the same fixed-point "which items are freely obtainable"
 *      propagation as `validate_puzzle_dependencies` and the same
 *      white/gray/black DFS cycle check as `detect_circular_dependencies`
 *      (`has_cycle`), generalized to arbitrary bitmask-sized graphs instead
 *      of Python dicts/sets.
 *
 * PIN early-abort is only ever enabled once an attempt's acceptance is
 * PROVEN (path 1 above) -- mirroring search.py's `_run_attempt_fast`
 * (`abort_enabled` only flips true after the same checkpoint). On the (rare,
 * ~8% of seeds) path where an attempt is rejected by the real game, the
 * retry loop moves on to `NetRandom(seed + attempt + 1)` exactly like
 * `simulate_verbose`, so the kernel's FINAL placement for a seed is always
 * the same one `simulator.simulate()` would produce -- no attempt-1-only
 * approximation remains.
 *
 * Every host-side struct field / bitmask this kernel consumes is documented
 * in gpu_backend.py's SceneLayout class, which builds them by directly
 * calling simulator.py's own validated predicate functions -- never by
 * re-deriving name-matching logic independently.
 *
 * Requires cl_khr_fp64 (System.Random's Sample() is a double-precision
 * computation and bit-exactness against simulator.py depends on IEEE-754
 * double arithmetic; float64 truncation error would silently produce wrong
 * seeds). gpu_backend.py refuses to run on a device lacking this extension
 * rather than silently degrading to float.
 *
 * Slot-id encoding (shared with gpu_backend.py's SceneLayout):
 *   0 .. NUM_PUZZLES-1                      -> PUZZLE:<puzzleDefs[i].puzzleName>
 *   NUM_PUZZLES .. NUM_PUZZLES+TOTAL_FREE-1 -> FREE:<area>/<spot>, computed as
 *       NUM_PUZZLES + area_spot_offset[a] + (index of the spot within area a's
 *       freeSpots list, in that list's original order)
 */

#pragma OPENCL EXTENSION cl_khr_fp64 : enable
#pragma OPENCL EXTENSION cl_khr_global_int32_base_atomics : enable

/* ---------------------------------------------------------------------
 * Compile-time scene configuration. gpu_backend.py ALWAYS overrides every
 * one of these via -D build options to match the loaded config exactly;
 * the values below only matter if this file is compiled standalone (e.g.
 * offline syntax checking) and are the current live scene's real values.
 * ------------------------------------------------------------------- */
#ifndef NUM_ITEMS
#define NUM_ITEMS 31        /* hard cap: item bitmasks are 32-bit (see re_gpu.md) */
#endif
#ifndef NUM_PUZZLES
#define NUM_PUZZLES 16      /* hard cap: puzzle bitmasks are 32-bit */
#endif
#ifndef NUM_AREAS
#define NUM_AREAS 12
#endif
#ifndef NUM_ESCAPE
#define NUM_ESCAPE 14
#endif

/* Item-indexed bitmask width. gpu_backend.py selects 64 only when a scene
 * has more than 32 items (see gpu_backend.py's SceneLayout); everything
 * else (area-spot masks, puzzle-indexed masks) stays a fixed 32-bit uint
 * regardless of this setting -- see kernel.cl's module docstring / the
 * mask taxonomy in re_gpu.md. */
#ifndef ITEM_MASK_BITS
#define ITEM_MASK_BITS 32
#endif
#if ITEM_MASK_BITS == 64
typedef ulong imask_t;
#define IBIT(i) (((ulong)1) << (i))
#else
typedef uint imask_t;
#define IBIT(i) (((uint)1) << (i))
#endif

#define MBIG 2147483647
#define MSEED 161803398
#define INT32_MIN_C (-2147483647 - 1)

/* =======================================================================
 * NetRandom -- bit-exact port of net_random.py. See that file's docstring
 * for the full provenance/derivation notes; this is a line-for-line port.
 * ===================================================================== */

typedef struct {
    int seedArray[56];
    int inext;
    int inextp;
} RndState;

static inline int wrap32(long x) {
    /* Reinterpret the low 32 bits of x as signed int32, matching
     * net_random.py's _wrap32. Every call site below already produces a
     * value that fits in int32 range without truncation (verified by
     * range analysis in re_gpu.md), so this is a zero-cost safety net
     * kept for line-for-line fidelity with the reference port, not because
     * it changes behavior on this data. */
    return (int)(uint)(x & 0xFFFFFFFFL);
}

static void net_random_init(RndState* r, int seed) {
    int subtraction = (seed == INT32_MIN_C) ? MBIG : abs(seed);
    int mj = wrap32((long)MSEED - (long)subtraction);

    int seedArray[56];
    for (int i = 0; i < 56; i++) seedArray[i] = 0;
    seedArray[55] = mj;

    int mk = 1;
    for (int i = 1; i < 55; i++) {
        int ii = (21 * i) % 55;
        seedArray[ii] = mk;
        mk = wrap32((long)mj - (long)mk);
        if (mk < 0) mk += MBIG;
        mj = seedArray[ii];
    }

    for (int k = 1; k < 5; k++) {          /* exactly 4 warm-up passes */
        for (int i = 1; i < 56; i++) {
            int v = wrap32((long)seedArray[i] - (long)seedArray[1 + (i + 30) % 55]);
            if (v < 0) v += MBIG;
            seedArray[i] = v;
        }
    }

    for (int i = 0; i < 56; i++) r->seedArray[i] = seedArray[i];
    r->inext = 0;
    r->inextp = 21;
}

static inline int net_random_internal_sample(RndState* r) {
    int loc_inext = r->inext + 1;
    if (loc_inext >= 56) loc_inext = 1;
    int loc_inextp = r->inextp + 1;
    if (loc_inextp >= 56) loc_inextp = 1;

    int ret = r->seedArray[loc_inext] - r->seedArray[loc_inextp];
    if (ret == MBIG) ret -= 1;
    if (ret < 0) ret += MBIG;

    r->seedArray[loc_inext] = ret;
    r->inext = loc_inext;
    r->inextp = loc_inextp;
    return ret;
}

static inline double net_random_sample(RndState* r) {
    return (double)net_random_internal_sample(r) * (1.0 / (double)MBIG);
}

static inline int net_random_next_int(RndState* r, int maxValue) {
    /* maxValue is always >= 1 at every call site below (mirrors
     * simulator.py / search.py, which only ever call next_int on a
     * non-empty pool -- see re_generateplacement.md). */
    double s = net_random_sample(r);
    return (int)(s * (double)maxValue); /* trunc toward zero; s*maxValue >= 0 */
}

static inline double net_random_next_double(RndState* r) {
    return net_random_sample(r);
}

/* =======================================================================
 * Bit helpers
 * ===================================================================== */

static inline int nth_set_bit_u(uint mask, int n) {
    /* Returns the bit index of the n-th (0-based) set bit of mask.
     * Portable O(32) scan -- deliberately avoids OpenCL-2.0-only ctz() so
     * this kernel also runs on OpenCL-1.2-only devices (older NVIDIA
     * cards), per the "vendor-neutral" requirement. Cheap: item/puzzle/
     * spot counts here are all <= 32. Used for category-(B)/(C) masks
     * (area-spot / puzzle-indexed), which are always 32-bit regardless of
     * ITEM_MASK_BITS. */
    for (int i = 0; i < 32; i++) {
        if (mask & (1u << i)) {
            if (n == 0) return i;
            n--;
        }
    }
    return -1; /* unreachable if n < popcount(mask); caller never violates this */
}

static inline int nth_set_bit_i(imask_t mask, int n) {
    /* Returns the bit index of the n-th (0-based) set bit of mask.
     * Portable O(ITEM_MASK_BITS) scan -- deliberately avoids
     * OpenCL-2.0-only ctz() so this kernel also runs on OpenCL-1.2-only
     * devices (older NVIDIA cards), per the "vendor-neutral" requirement.
     * Used for category-(A) item-indexed masks, which widen to 64-bit when
     * ITEM_MASK_BITS == 64. */
    for (int i = 0; i < ITEM_MASK_BITS; i++) {
        if (mask & IBIT(i)) {
            if (n == 0) return i;
            n--;
        }
    }
    return -1; /* unreachable if n < popcount(mask); caller never violates this */
}

/* =======================================================================
 * Placement primitives -- direct ports of the like-named closures in
 * search.py's `_attempt1_fast` (itself proven equivalent to
 * simulator.py's simulate_verbose). Mutable state is threaded through by
 * pointer since OpenCL C has no closures.
 * ===================================================================== */

/* Port of place_item_in_free_area(item). Returns true iff placed.
 * Does NOT touch used_mask -- callers apply mark_used() semantics
 * themselves, because the reference implementation is INCONSISTENT about
 * whether mark_used() is unconditional or gated on success depending on
 * the call site (Step B/E: unconditional: Step F: gated) -- a genuine
 * quirk of the original code, faithfully preserved, not "fixed". */
static bool place_item_in_free_area(
    RndState* rnd, int item_idx,
    int* area_current, uint* area_used_mask,
    __global const int* area_allow_all, __global const imask_t* area_allow_mask,
    __global const int* area_max_items, __global const int* area_num_spots,
    __global const int* area_spot_offset,
    int* result_slot,
    imask_t pin_mask, __global const int* pin_target, bool* reject)
{
    int elig[NUM_AREAS];
    int n_elig = 0;
    for (int a = 0; a < NUM_AREAS; a++) {
        if (area_current[a] < area_max_items[a] && area_num_spots[a] != 0 &&
            (area_allow_all[a] || (area_allow_mask[a] & IBIT(item_idx)))) {
            elig[n_elig++] = a;
        }
    }
    /* Fisher-Yates shuffle of the eligible-areas list (RNG-significant). */
    for (int i = n_elig - 1; i > 0; i--) {
        int j = net_random_next_int(rnd, i + 1);
        int tmp = elig[i]; elig[i] = elig[j]; elig[j] = tmp;
    }
    for (int k = 0; k < n_elig; k++) {
        int a = elig[k];
        uint validSpots = (area_num_spots[a] >= 32) ? 0xFFFFFFFFu
                                                     : ((1u << area_num_spots[a]) - 1u);
        uint freeSpots = validSpots & ~area_used_mask[a];
        if (freeSpots != 0u) {
            int freeCount = popcount(freeSpots);
            int idx = net_random_next_int(rnd, freeCount);
            int spotPos = nth_set_bit_u(freeSpots, idx);
            area_current[a] += 1;
            area_used_mask[a] |= (1u << spotPos);
            int slot_id = NUM_PUZZLES + area_spot_offset[a] + spotPos;
            result_slot[item_idx] = slot_id;
            if ((pin_mask & IBIT(item_idx)) && pin_target[item_idx] != slot_id) *reject = true;
            return true;
        }
    }
    /* Fallback: linear scan over the ORIGINAL (unshuffled) area order, with
     * NO allowedItemNames filter -- matches simulator.py's fallback
     * literally (a deliberate quirk, not a bug). */
    for (int a = 0; a < NUM_AREAS; a++) {
        if (!(area_current[a] < area_max_items[a])) continue;
        uint validSpots = (area_num_spots[a] >= 32) ? 0xFFFFFFFFu
                                                     : ((1u << area_num_spots[a]) - 1u);
        uint freeSpots = validSpots & ~area_used_mask[a];
        if (freeSpots != 0u) {
            int freeCount = popcount(freeSpots);
            int idx = net_random_next_int(rnd, freeCount);
            int spotPos = nth_set_bit_u(freeSpots, idx);
            area_current[a] += 1;
            area_used_mask[a] |= (1u << spotPos);
            int slot_id = NUM_PUZZLES + area_spot_offset[a] + spotPos;
            result_slot[item_idx] = slot_id;
            if ((pin_mask & IBIT(item_idx)) && pin_target[item_idx] != slot_id) *reject = true;
            return true;
        }
    }
    return false;
}

/* Port of place_item_in_specific_free_area(item, area) -- Step E only. */
static bool place_item_in_specific_free_area(
    RndState* rnd, int item_idx, int area_idx,
    int* area_current, uint* area_used_mask,
    __global const int* area_num_spots, __global const int* area_spot_offset,
    int* result_slot,
    imask_t pin_mask, __global const int* pin_target, bool* reject)
{
    uint validSpots = (area_num_spots[area_idx] >= 32) ? 0xFFFFFFFFu
                                                        : ((1u << area_num_spots[area_idx]) - 1u);
    uint freeSpots = validSpots & ~area_used_mask[area_idx];
    if (freeSpots == 0u) return false;
    int freeCount = popcount(freeSpots);
    int idx = net_random_next_int(rnd, freeCount);
    int spotPos = nth_set_bit_u(freeSpots, idx);
    area_current[area_idx] += 1;
    area_used_mask[area_idx] |= (1u << spotPos);
    int slot_id = NUM_PUZZLES + area_spot_offset[area_idx] + spotPos;
    result_slot[item_idx] = slot_id;
    if ((pin_mask & IBIT(item_idx)) && pin_target[item_idx] != slot_id) *reject = true;
    return true;
}

/* Port of the place_puzzle(item, puzzleName, spawnPointId) closure used in
 * Steps B/C/D. Always unconditional (no failure mode), matching the
 * reference: puzzle slot ids are simply the puzzle's own original index. */
static void place_puzzle(
    int item_idx, int puzzle_idx,
    int* result_slot, imask_t* used_mask, uint* puzzle_used_mask,
    imask_t pin_mask, __global const int* pin_target, bool* reject)
{
    int slot_id = puzzle_idx;
    result_slot[item_idx] = slot_id;
    *used_mask |= IBIT(item_idx);
    *puzzle_used_mask |= (1u << puzzle_idx);
    if ((pin_mask & IBIT(item_idx)) && pin_target[item_idx] != slot_id) *reject = true;
}

/* =======================================================================
 * Acceptance-test primitives -- direct ports of search.py's
 * `_fast_effective_puzzle_resolver` / `validate_puzzle_dependencies` /
 * `detect_circular_dependencies` (themselves ports of simulator.py's own
 * unmodified functions), generalized to bitmask/array form. Only ever
 * invoked from the (rare) path where a container item reached a puzzle
 * spawn -- see the module docstring's "REAL CHECK" section.
 * ===================================================================== */

/* Port of GetEffectivePuzzleOfItem, restricted to this attempt's placement.
 * A container's CURRENT slot id tells us exactly and losslessly whether it
 * is sitting at a puzzle spawn (slot_id < NUM_PUZZLES) and precisely which
 * one (slot_id itself, since puzzle slot ids ARE the puzzle's own index) --
 * no floating-point spatial distance computation is needed, unlike
 * simulator.py's literal implementation, because a container is only ever
 * "at" a puzzle's spawn point by actually being assigned there (Steps A-D
 * are the only placement code that can ever move an item onto a puzzle
 * slot), and this scene's free-spot positions are never within 0.01 units
 * of any puzzle's spawn point (verified indirectly: search.py's own
 * position-based implementation and this slot-id-based one were checked to
 * agree on every one of the several-thousand-seed cross-validation runs
 * behind this fix -- see re_gpu.md). BFS over the "contained-by" graph
 * (instead of simulator.py's recursion) generalizes correctly to nested
 * containers (an item contained by a container that is itself contained by
 * another), though this scene has exactly one container ("Melon") so
 * nesting is never actually exercised by real data -- same caveat
 * search.py's own resolver docs already carry. */
static int resolve_effective_puzzle_bfs(
    int start_item_idx,
    int* result_slot, /* private, NUM_ITEMS -- this attempt's placement so far */
    __global const int* container_item_idx,
    __global const imask_t* container_contains_mask,
    int num_containers)
{
    imask_t visited = IBIT(start_item_idx);
    int queue[NUM_ITEMS];
    int qhead = 0, qtail = 0;
    queue[qtail++] = start_item_idx;
    while (qhead < qtail) {
        int cur = queue[qhead++];
        for (int c = 0; c < num_containers; c++) {
            if (!(container_contains_mask[c] & IBIT(cur))) continue;
            int g = container_item_idx[c];
            int slot = result_slot[g];
            if (slot >= 0 && slot < NUM_PUZZLES) return slot;
            if (!(visited & IBIT(g)) && qtail < NUM_ITEMS) {
                visited |= IBIT(g);
                queue[qtail++] = g;
            }
        }
    }
    return -1; /* freely obtainable (or not reachable via any container) */
}

/* Port of DetectCircularDependencies's white/gray/black DFS, generalized to
 * a fixed bipartite graph over NUM_ITEMS item-nodes (0..NUM_ITEMS-1, each
 * with at most one outgoing edge: Item->Puzzle iff effective_puzzle_of[i] is
 * not -1) and NUM_PUZZLES puzzle-nodes (NUM_ITEMS..NUM_ITEMS+NUM_PUZZLES-1,
 * each with static outgoing edges Puzzle->Item for every bit set in
 * puzzle_requires_mask[p]) -- an iterative (non-recursive) equivalent of
 * simulator.py's `dfs`, since OpenCL C recursion is not portable across
 * vendors. */
#define NUM_GRAPH_NODES (NUM_ITEMS + NUM_PUZZLES)
static bool has_cycle(int* effective_puzzle_of, __global const imask_t* puzzle_requires_mask)
{
    int color[NUM_GRAPH_NODES];
    int stack[NUM_GRAPH_NODES];
    int cursor[NUM_GRAPH_NODES];
    for (int i = 0; i < NUM_GRAPH_NODES; i++) color[i] = 0;

    for (int start = 0; start < NUM_GRAPH_NODES; start++) {
        if (color[start] != 0) continue;
        int sp = 0;
        color[start] = 1;
        cursor[start] = 0;
        stack[sp++] = start;
        while (sp > 0) {
            int node = stack[sp - 1];
            int nextNode = -1;
            if (node < NUM_ITEMS) {
                if (cursor[node] == 0) {
                    cursor[node] = 1;
                    int p = effective_puzzle_of[node];
                    if (p >= 0) nextNode = NUM_ITEMS + p;
                }
            } else {
                int p = node - NUM_ITEMS;
                imask_t mask = puzzle_requires_mask[p];
                while (cursor[node] < NUM_ITEMS && !(mask & IBIT(cursor[node]))) cursor[node]++;
                if (cursor[node] < NUM_ITEMS) {
                    nextNode = cursor[node];
                    cursor[node]++;
                }
            }
            if (nextNode == -1) {
                color[node] = 2;
                sp--;
            } else {
                int c = color[nextNode];
                if (c == 0) {
                    color[nextNode] = 1;
                    cursor[nextNode] = 0;
                    stack[sp++] = nextNode;
                } else if (c == 1) {
                    return true; /* back-edge to a node still on the stack -- cycle */
                }
                /* c == 2: already fully explored -- forward/cross edge, ignore */
            }
        }
    }
    return false;
}

/* Port of `!detect_circular_dependencies() && validate_puzzle_dependencies()`
 * -- the real acceptance test, only ever called from the path where
 * `guaranteed_accept` (see `simulate_one_attempt`) could not be proven. */
static bool compute_real_acceptance(
    int* result_slot, /* private, NUM_ITEMS -- this attempt's FINAL placement */
    __global const int* container_item_idx,
    __global const imask_t* container_contains_mask,
    int num_containers,
    __global const imask_t* puzzle_requires_mask,
    __global const int*  puzzle_active,
    imask_t required_item_mask)
{
    int effective_puzzle_of[NUM_ITEMS];
    imask_t obtainable_mask = (imask_t)0;
    imask_t puzzle_unlocks_mask[NUM_PUZZLES];
    for (int p = 0; p < NUM_PUZZLES; p++) puzzle_unlocks_mask[p] = (imask_t)0;

    for (int i = 0; i < NUM_ITEMS; i++) {
        int ep = resolve_effective_puzzle_bfs(i, result_slot, container_item_idx,
                                               container_contains_mask, num_containers);
        effective_puzzle_of[i] = ep;
        if (ep < 0) {
            obtainable_mask |= IBIT(i);
        } else {
            puzzle_unlocks_mask[ep] |= IBIT(i);
        }
    }

    /* Fixed-point propagation, port of validate_puzzle_dependencies's
     * `while changed:` loop. Bounded to NUM_PUZZLES passes: each pass that
     * makes progress processes >= 1 new puzzle, and there are at most
     * NUM_PUZZLES puzzles total. */
    bool processed[NUM_PUZZLES];
    for (int p = 0; p < NUM_PUZZLES; p++) processed[p] = false;
    for (int pass = 0; pass < NUM_PUZZLES; pass++) {
        bool changed = false;
        for (int p = 0; p < NUM_PUZZLES; p++) {
            if (processed[p] || !puzzle_active[p]) continue;
            if ((puzzle_requires_mask[p] & ~obtainable_mask) == (imask_t)0) {
                processed[p] = true;
                obtainable_mask |= puzzle_unlocks_mask[p];
                changed = true;
            }
        }
        if (!changed) break;
    }

    for (int i = 0; i < NUM_ITEMS; i++) {
        if (!(required_item_mask & IBIT(i))) continue;
        if (!(obtainable_mask & IBIT(i)) && effective_puzzle_of[i] >= 0) {
            return false; /* ValidatePuzzleDependencies would return False */
        }
    }

    return !has_cycle(effective_puzzle_of, puzzle_requires_mask);
}

/* =======================================================================
 * One attempt of GeneratePlacement (Steps A-F), plus the acceptance test
 * (see module docstring). `attempt` is 1-based, matching
 * `NetRandom(seed + attempt)` in simulate_verbose. Returns whether the real
 * game accepts this attempt (proven analytically, or computed for real --
 * see docstring); `*mismatch_out` reports whether any PIN-constrained item
 * ended up on a slot other than its pinned target IN THIS ATTEMPT'S final
 * placement -- only meaningful (i.e. only reflects the seed's true fate)
 * when the return value is true, exactly like search.py's `_run_attempt_fast`
 * returning `(result, accepted)`. */
static bool simulate_one_attempt(
    long seed, int attempt,
    imask_t category_mask,
    __global const imask_t* puzzle_allow_mask,
    __global const imask_t* puzzle_tier1_mask,
    __global const imask_t* puzzle_candidate_mask,
    __global const int*  puzzle_order,
    __global const int*  escape_item_idx,
    double escape_chance,
    __global const int*  area_allow_all,
    __global const imask_t* area_allow_mask,
    __global const int*  area_max_items,
    __global const int*  area_num_spots,
    __global const int*  area_spot_offset,
    __global const int*  area_mandatory_item_idx,
    int fill_all,
    __global const int*  container_item_idx,
    __global const imask_t* container_contains_mask,
    int num_containers,
    __global const imask_t* puzzle_requires_mask,
    __global const int*  puzzle_active,
    imask_t required_item_mask,
    imask_t pin_mask,
    __global const int*  pin_target,
    int* result_slot, /* [NUM_ITEMS] out, -1 == unplaced */
    bool* mismatch_out)
{
    RndState rnd;
    long netseed64 = seed + (long)attempt;   /* NetRandom(seed + attempt) */
    net_random_init(&rnd, wrap32(netseed64));

    for (int i = 0; i < NUM_ITEMS; i++) result_slot[i] = -1;

    int area_current[NUM_AREAS];
    uint area_used_mask[NUM_AREAS];
    for (int a = 0; a < NUM_AREAS; a++) { area_current[a] = 0; area_used_mask[a] = 0u; }

    imask_t used_mask = (imask_t)0;        /* used_item_names */
    imask_t chosen_a_mask = (imask_t)0;    /* items chosen during Step A == assigned_names */
    uint puzzle_used_mask = 0u; /* used_puzzle_spawns, indexed by original puzzle index */
    int assigned_item_idx[NUM_PUZZLES];
    for (int p = 0; p < NUM_PUZZLES; p++) assigned_item_idx[p] = -1;

    bool reject = false;

    /* ---- Step A: greedy puzzle<-item assignment (priority order) ---- */
    for (int oi = 0; oi < NUM_PUZZLES; oi++) {
        int p = puzzle_order[oi];
        imask_t pool = puzzle_candidate_mask[p] & ~chosen_a_mask;
        if (pool != (imask_t)0) {
            int cnt = popcount(pool);
            int idx = net_random_next_int(&rnd, cnt);
            int chosen = nth_set_bit_i(pool, idx);
            assigned_item_idx[p] = chosen;
            chosen_a_mask |= IBIT(chosen);
        }
    }

    /* ---- Step B: required escape items ----
     * NOTE: no early-abort on `reject` here (and none in Steps C/D below) --
     * acceptance is not yet provable this early (see `guaranteed_accept`
     * below), so a PIN mismatch here is only ever *recorded*, matching
     * search.py's `check()` before `abort_enabled` flips true. */
    for (int e = 0; e < NUM_ESCAPE; e++) {
        int item_idx = escape_item_idx[e];
        if (chosen_a_mask & IBIT(item_idx)) continue; /* already assigned in Step A */

        double roll = net_random_next_double(&rnd);
        if (escape_chance <= roll) {
            place_item_in_free_area(&rnd, item_idx, area_current, area_used_mask,
                area_allow_all, area_allow_mask, area_max_items, area_num_spots, area_spot_offset,
                result_slot, pin_mask, pin_target, &reject);
            used_mask |= IBIT(item_idx); /* unconditional -- matches reference quirk */
        } else {
#if NUM_PUZZLES == 0
            place_item_in_free_area(&rnd, item_idx, area_current, area_used_mask,
                area_allow_all, area_allow_mask, area_max_items, area_num_spots, area_spot_offset,
                result_slot, pin_mask, pin_target, &reject);
            used_mask |= IBIT(item_idx);
#else
            int elig[NUM_PUZZLES];
            int n_elig = 0;
            for (int p = 0; p < NUM_PUZZLES; p++) {
                if (!(puzzle_used_mask & (1u << p)) && assigned_item_idx[p] == -1 &&
                    (puzzle_allow_mask[p] & IBIT(item_idx))) {
                    elig[n_elig++] = p;
                }
            }
            if (n_elig > 0) {
                int idx = net_random_next_int(&rnd, n_elig);
                int p = elig[idx];
                place_puzzle(item_idx, p, result_slot, &used_mask, &puzzle_used_mask,
                             pin_mask, pin_target, &reject);
            } else {
                place_item_in_free_area(&rnd, item_idx, area_current, area_used_mask,
                    area_allow_all, area_allow_mask, area_max_items, area_num_spots, area_spot_offset,
                    result_slot, pin_mask, pin_target, &reject);
                used_mask |= IBIT(item_idx);
            }
#endif
        }
    }

    /* ---- Step C: commit Step-A assignments ---- */
    for (int oi = 0; oi < NUM_PUZZLES; oi++) {
        int p = puzzle_order[oi];
        if (!(puzzle_used_mask & (1u << p)) && assigned_item_idx[p] != -1) {
            int item_idx = assigned_item_idx[p];
            if (used_mask & IBIT(item_idx)) {
                assigned_item_idx[p] = -1;
            } else {
                place_puzzle(item_idx, p, result_slot, &used_mask, &puzzle_used_mask,
                             pin_mask, pin_target, &reject);
            }
        }
    }

    /* ---- Step D: backfill still-empty puzzles ---- */
    for (int oi = 0; oi < NUM_PUZZLES; oi++) {
        int p = puzzle_order[oi];
        if (!(puzzle_used_mask & (1u << p))) {
            imask_t pool = puzzle_tier1_mask[p] & ~used_mask;
            if (pool == (imask_t)0) pool = category_mask & ~used_mask;
            if (pool != (imask_t)0) {
                int cnt = popcount(pool);
                int idx = net_random_next_int(&rnd, cnt);
                int item_idx = nth_set_bit_i(pool, idx);
                place_puzzle(item_idx, p, result_slot, &used_mask, &puzzle_used_mask,
                             pin_mask, pin_target, &reject);
            }
        }
    }

    /* ---- Provably-safe acceptance checkpoint (see module docstring) ----
     * All puzzle placements happen in Steps A-D; Steps E/F only ever place
     * items into FREE spots. So this is already the FINAL truth, for this
     * attempt, of whether any container reached a puzzle spawn. */
    bool guaranteed_accept = true;
    for (int c = 0; c < num_containers; c++) {
        int gslot = result_slot[container_item_idx[c]];
        if (gslot >= 0 && gslot < NUM_PUZZLES) {
            guaranteed_accept = false;
            break;
        }
    }

    if (guaranteed_accept && reject) {
        /* Proven accepted, and a PIN already mismatched somewhere in Steps
         * A-D -- this attempt's fate is sealed either way, so this is a
         * safe, definitive rejection (mirrors search.py's gate). */
        *mismatch_out = true;
        return true;
    }

    /* ---- Step E: mandatory-item spawn areas (dead in the live scene's
     * config -- mandatoryItemName is always empty there -- but ported for
     * correctness in case a future config sets it). */
    for (int a = 0; a < NUM_AREAS; a++) {
        int m = area_mandatory_item_idx[a];
        if (m >= 0 && !(used_mask & IBIT(m))) {
            place_item_in_specific_free_area(&rnd, m, a, area_current, area_used_mask,
                area_num_spots, area_spot_offset, result_slot, pin_mask, pin_target, &reject);
            used_mask |= IBIT(m); /* unconditional -- matches reference quirk */
            if (guaranteed_accept && reject) { *mismatch_out = true; return true; }
        }
    }

    /* ---- Step F: fill remaining free spawns ---- */
    if (fill_all) {
        int remaining[NUM_ITEMS];
        int n_remaining = 0;
        for (int i = 0; i < NUM_ITEMS; i++) {
            if (!(used_mask & IBIT(i))) remaining[n_remaining++] = i;
        }
        for (int i = n_remaining - 1; i > 0; i--) {
            int j = net_random_next_int(&rnd, i + 1);
            int tmp = remaining[i]; remaining[i] = remaining[j]; remaining[j] = tmp;
        }
        for (int k = 0; k < n_remaining; k++) {
            int item_idx = remaining[k];
            bool placed = place_item_in_free_area(&rnd, item_idx, area_current, area_used_mask,
                area_allow_all, area_allow_mask, area_max_items, area_num_spots, area_spot_offset,
                result_slot, pin_mask, pin_target, &reject);
            if (placed) used_mask |= IBIT(item_idx); /* gated -- matches Step F semantics */
            if (guaranteed_accept && reject) { *mismatch_out = true; return true; }
        }
    }

    if (guaranteed_accept) {
        /* Proven above -- no need to run the real acceptance check at all. */
        *mismatch_out = reject; /* false, since we'd have returned above otherwise */
        return true;
    }

    /* ---- Real acceptance check: only reached when a container reached a
     * puzzle spawn (the ~8% of attempts, per re_search.md's measurement,
     * where the fast path above cannot be proven). ---- */
    *mismatch_out = reject;
    return compute_real_acceptance(result_slot, container_item_idx, container_contains_mask,
                                    num_containers, puzzle_requires_mask, puzzle_active,
                                    required_item_mask);
}

/* =======================================================================
 * Full retry loop: replays simulate_verbose's own `while attempt <
 * max_attempts and not success:` loop attempt-for-attempt, stopping at the
 * first accepted attempt (or, matching simulate_verbose's own -- in practice
 * unobserved -- fallthrough behavior, using the last attempt's partial
 * result after 50 tries). `*pin_violated` reflects whichever attempt's
 * placement `result_slot` ends up holding: the accepted attempt's, or (in
 * the fallthrough case) the 50th attempt's, exactly mirroring
 * search.py's `_run_fast_multi_attempt`/`evaluate_seed`.
 * ===================================================================== */
static void simulate_seed(
    long seed,
    imask_t category_mask,
    __global const imask_t* puzzle_allow_mask,
    __global const imask_t* puzzle_tier1_mask,
    __global const imask_t* puzzle_candidate_mask,
    __global const int*  puzzle_order,
    __global const int*  escape_item_idx,
    double escape_chance,
    __global const int*  area_allow_all,
    __global const imask_t* area_allow_mask,
    __global const int*  area_max_items,
    __global const int*  area_num_spots,
    __global const int*  area_spot_offset,
    __global const int*  area_mandatory_item_idx,
    int fill_all,
    __global const int*  container_item_idx,
    __global const imask_t* container_contains_mask,
    int num_containers,
    __global const imask_t* puzzle_requires_mask,
    __global const int*  puzzle_active,
    imask_t required_item_mask,
    imask_t pin_mask,
    __global const int*  pin_target,
    int* result_slot, /* [NUM_ITEMS] out, -1 == unplaced */
    bool* pin_violated)
{
    bool mismatch = true;
    for (int attempt = 1; attempt <= 50; attempt++) {
        bool accepted = simulate_one_attempt(seed, attempt, category_mask,
            puzzle_allow_mask, puzzle_tier1_mask, puzzle_candidate_mask, puzzle_order,
            escape_item_idx, escape_chance, area_allow_all, area_allow_mask, area_max_items,
            area_num_spots, area_spot_offset, area_mandatory_item_idx, fill_all,
            container_item_idx, container_contains_mask, num_containers,
            puzzle_requires_mask, puzzle_active, required_item_mask,
            pin_mask, pin_target, result_slot, &mismatch);
        if (accepted) {
            *pin_violated = mismatch;
            return;
        }
    }
    /* Fell through 50 attempts without acceptance (in practice unobserved,
     * per re_search.md) -- use the last attempt's partial result/mismatch
     * state, matching simulate_verbose's own fallthrough. */
    *pin_violated = mismatch;
}

/* =======================================================================
 * Entry points
 * ===================================================================== */

/* Computes the FULL retry-loop placement (attempts 1..50, exactly matching
 * `simulator.simulate()`) for every seed in
 * [seed_start, seed_start+seed_count) with NO pin filtering (pin_mask must
 * be 0 and pin_target may point at any valid NUM_ITEMS-sized buffer, its
 * contents are never read when pin_mask == 0). Writes NUM_ITEMS slot ids
 * per seed to out_slots, row-major. Used exclusively by test_gpu.py for
 * bit-exactness validation against simulator.py -- never used by the
 * production search path (which needs pin-based early rejection to be
 * fast, see search_kernel below). */
__kernel void debug_placement(
    long seed_start, uint seed_count,
    imask_t category_mask,
    __global const imask_t* puzzle_allow_mask,
    __global const imask_t* puzzle_tier1_mask,
    __global const imask_t* puzzle_candidate_mask,
    __global const int*  puzzle_order,
    __global const int*  escape_item_idx,
    double escape_chance,
    __global const int*  area_allow_all,
    __global const imask_t* area_allow_mask,
    __global const int*  area_max_items,
    __global const int*  area_num_spots,
    __global const int*  area_spot_offset,
    __global const int*  area_mandatory_item_idx,
    int fill_all,
    __global const int*  container_item_idx,
    __global const imask_t* container_contains_mask,
    int num_containers,
    __global const imask_t* puzzle_requires_mask,
    __global const int*  puzzle_active,
    imask_t required_item_mask,
    imask_t pin_mask,
    __global const int*  pin_target,
    __global int* out_slots)
{
    size_t gid = get_global_id(0);
    if (gid >= seed_count) return;
    long seed64 = seed_start + (long)gid;

    int result_slot[NUM_ITEMS];
    bool pin_violated;
    simulate_seed(seed64, category_mask, puzzle_allow_mask, puzzle_tier1_mask,
        puzzle_candidate_mask, puzzle_order, escape_item_idx, escape_chance,
        area_allow_all, area_allow_mask, area_max_items, area_num_spots, area_spot_offset,
        area_mandatory_item_idx, fill_all,
        container_item_idx, container_contains_mask, num_containers,
        puzzle_requires_mask, puzzle_active, required_item_mask,
        pin_mask, pin_target, result_slot, &pin_violated);

    for (int i = 0; i < NUM_ITEMS; i++) {
        out_slots[(size_t)gid * NUM_ITEMS + i] = result_slot[i];
    }
}

/* Production search: one work item per seed. Appends `seed` (the raw
 * int32 seed value, NOT gid) to hit_seeds via atomic_inc(hit_count) iff the
 * FULL retry-loop placement (attempts 1..50, the same one
 * `simulator.simulate()` would produce) satisfies every PIN constraint.
 * hit_count is incremented even past max_hits so the host can always detect
 * overflow ("buffer full") and never silently drop a result -- see
 * gpu_backend.py. */
__kernel void search_kernel(
    long seed_start, uint seed_count,
    imask_t category_mask,
    __global const imask_t* puzzle_allow_mask,
    __global const imask_t* puzzle_tier1_mask,
    __global const imask_t* puzzle_candidate_mask,
    __global const int*  puzzle_order,
    __global const int*  escape_item_idx,
    double escape_chance,
    __global const int*  area_allow_all,
    __global const imask_t* area_allow_mask,
    __global const int*  area_max_items,
    __global const int*  area_num_spots,
    __global const int*  area_spot_offset,
    __global const int*  area_mandatory_item_idx,
    int fill_all,
    __global const int*  container_item_idx,
    __global const imask_t* container_contains_mask,
    int num_containers,
    __global const imask_t* puzzle_requires_mask,
    __global const int*  puzzle_active,
    imask_t required_item_mask,
    imask_t pin_mask,
    __global const int*  pin_target,
    __global int* hit_seeds,
    uint max_hits,
    __global volatile uint* hit_count)
{
    size_t gid = get_global_id(0);
    if (gid >= seed_count) return;
    long seed64 = seed_start + (long)gid;
    int seed = (int)seed64;

    int result_slot[NUM_ITEMS];
    bool pin_violated;
    simulate_seed(seed64, category_mask, puzzle_allow_mask, puzzle_tier1_mask,
        puzzle_candidate_mask, puzzle_order, escape_item_idx, escape_chance,
        area_allow_all, area_allow_mask, area_max_items, area_num_spots, area_spot_offset,
        area_mandatory_item_idx, fill_all,
        container_item_idx, container_contains_mask, num_containers,
        puzzle_requires_mask, puzzle_active, required_item_mask,
        pin_mask, pin_target, result_slot, &pin_violated);

    if (!pin_violated) {
        uint idx = atomic_inc(hit_count);
        if (idx < max_hits) {
            hit_seeds[idx] = seed;
        }
    }
}
