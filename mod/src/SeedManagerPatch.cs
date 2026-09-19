using System;
using System.Collections.Generic;
using System.Threading;
using HarmonyLib;
using Il2Cpp;
using MelonLoader;
using UnityEngine;

namespace SeedDumper
{
    /// <summary>
    /// Prefix dumps SeedManager's serialized configuration exactly as it
    /// stands right before GeneratePlacement runs (config_*.json) — this is
    /// the simulation input. Postfix dumps the resulting item placement right
    /// after (placement_*.json) — this is the ground truth a Python
    /// reimplementation of the algorithm must reproduce.
    ///
    /// Every collection is walked strictly in its original index order and
    /// never sorted: order determines RNG draw order in the real algorithm
    /// (see CLAUDE_FINDINGS.md), so reordering here would silently invalidate
    /// the dump as a comparison target.
    /// </summary>
    [HarmonyPatch(typeof(SeedManager), nameof(SeedManager.GeneratePlacement))]
    internal static class SeedManagerPatch
    {
        private static int _callCount;

        // A stack, not a single counter, so a hypothetical reentrant call
        // (GeneratePlacement calling itself) still pairs each Postfix with
        // the Prefix that started it, instead of silently mislabeling files.
        private static readonly Stack<int> CallStack = new Stack<int>();

        private static void Prefix(SeedManager __instance)
        {
            var call = Interlocked.Increment(ref _callCount);
            CallStack.Push(call);
            var log = SeedDumperMod.Log;
            try
            {
                log?.Msg($"[SeedDumper] GeneratePlacement call #{call}: dumping config (prefix)...");
                DumpConfig(__instance, call, log);
                log?.Msg($"[SeedDumper] GeneratePlacement call #{call}: config dump OK.");
            }
            catch (Exception e)
            {
                log?.Error($"[SeedDumper] GeneratePlacement call #{call}: config dump FAILED: {e}");
            }
        }

        private static void Postfix(SeedManager __instance)
        {
            var call = CallStack.Count > 0 ? CallStack.Pop() : _callCount;
            var log = SeedDumperMod.Log;
            try
            {
                log?.Msg($"[SeedDumper] GeneratePlacement call #{call}: dumping placement (postfix)...");
                DumpPlacement(__instance, call, log);
                log?.Msg($"[SeedDumper] GeneratePlacement call #{call}: placement dump OK.");
            }
            catch (Exception e)
            {
                log?.Error($"[SeedDumper] GeneratePlacement call #{call}: placement dump FAILED: {e}");
            }
        }

        // ------------------------------------------------------------------
        // config_*.json — the simulation input
        // ------------------------------------------------------------------

        private static void DumpConfig(SeedManager sm, int call, MelonLogger.Instance log)
        {
            int gameSeedPref = PlayerPrefs.GetInt("GameSeed");
            int randomSeedPref = PlayerPrefs.GetInt("RandomSeed");

            var w = new JsonWriter();
            w.BeginObject();

            w.Key("dumpType").Value("config");
            w.Key("callIndex").Value(call);
            w.Key("capturedAtUtc").Value(DateTime.UtcNow.ToString("o"));

            w.Key("seed").Value(sm.Seed);
            w.Key("randomizeSeed").Value(sm.RandomizeSeed);
            w.Key("activatePlacedItems").Value(sm.activatePlacedItems);
            w.Key("fillAllFreeSpawns").Value(sm.fillAllFreeSpawns);
            w.Key("rigid").Value(sm.Rigid);
            w.Key("escapeItemPuzzleChance").Value(sm.EscapeItemPuzzleChance);

            w.Key("playerPrefs").BeginObject();
            w.Key("GameSeed").Value(gameSeedPref);
            w.Key("RandomSeed").Value(randomSeedPref);
            w.EndObject();

            // allItems, in exact list order. Entries without ItemSeedData are
            // recorded with itemSeedData:null rather than skipped -- the
            // placement algorithm filters these out, so presence/absence is
            // itself information the Python side needs.
            w.Key("allItems").BeginArray();
            var items = sm.allItems;
            var itemCount = items != null ? items.Count : 0;
            for (var i = 0; i < itemCount; i++)
            {
                var go = items[i];
                w.Element().BeginObject();
                w.Key("index").Value(i);
                if (go == null)
                {
                    w.Key("name").Null();
                    w.Key("path").Null();
                    w.Key("itemSeedData").Null();
                }
                else
                {
                    w.Key("name").Value(go.name);
                    // Mirrors the placement dump's "name" field exactly (both
                    // are go.name), added under its own key so a config-only
                    // consumer doesn't need to special-case which dump it's
                    // reading to get the GameObject's actual name.
                    w.Key("goName").Value(go.name);
                    w.Key("path").Value(DumpUtil.FullPath(go.transform));

                    var startTransform = go.transform;
                    var startPos = startTransform != null ? startTransform.position : Vector3.zero;
                    w.Key("startPosition").BeginObject();
                    w.Key("x").Value(startPos.x);
                    w.Key("y").Value(startPos.y);
                    w.Key("z").Value(startPos.z);
                    w.EndObject();

                    ItemSeedData isd;
                    try { isd = go.GetComponent<ItemSeedData>(); }
                    catch { isd = null; }

                    w.Key("itemSeedData");
                    if (isd == null)
                    {
                        w.Null();
                    }
                    else
                    {
                        w.BeginObject();
                        w.Key("itemName").Value(isd.itemName);
                        w.Key("category").Value(isd.category);
                        w.Key("containedItems");
                        WriteStringList(w, isd.containedItems);
                        w.EndObject();
                    }
                }
                w.EndObject();
            }
            w.EndArray();

            w.Key("requiredEscapeItemNames");
            WriteStringList(w, sm.requiredEscapeItemNames);

            // puzzleDefs, in exact list order.
            w.Key("puzzleDefs").BeginArray();
            var puzzles = sm.puzzleDefs;
            var puzzleCount = puzzles != null ? puzzles.Count : 0;
            for (var i = 0; i < puzzleCount; i++)
            {
                var pd = puzzles[i];
                w.Element().BeginObject();
                w.Key("index").Value(i);
                if (pd == null)
                {
                    w.EndObject();
                    continue;
                }

                w.Key("puzzleName").Value(pd.puzzleName);
                w.Key("priority").Value(pd.Priority);
                w.Key("spawnPoint");
                WriteTransformRef(w, pd.spawnPoint, includePosition: true);
                w.Key("allowedItemNames");
                WriteStringList(w, pd.allowedItemNames);
                w.Key("excludeItemNames");
                WriteStringList(w, pd.excludeItemNames);
                w.Key("requiredItemNames");
                WriteStringList(w, pd.requiredItemNames);
                w.Key("containedItems");
                WriteStringList(w, pd.containedItems);

                w.Key("forbiddenCombos").BeginArray();
                var combos = pd.forbiddenCombos;
                var comboCount = combos != null ? combos.Count : 0;
                for (var j = 0; j < comboCount; j++)
                {
                    var c = combos[j];
                    w.Element().BeginObject();
                    if (c != null)
                    {
                        w.Key("itemName").Value(c.itemName);
                        w.Key("forbiddenItem").Value(c.forbiddenItem);
                        w.Key("forbiddenPuzzles");
                        WriteStringList(w, c.forbiddenPuzzles);
                    }
                    w.EndObject();
                }
                w.EndArray();

                w.EndObject();
            }
            w.EndArray();

            // spawnAreas, in exact array order.
            w.Key("spawnAreas").BeginArray();
            var areas = sm.spawnAreas;
            var areaCount = areas != null ? areas.Length : 0;
            for (var i = 0; i < areaCount; i++)
            {
                var area = areas[i];
                w.Element().BeginObject();
                w.Key("index").Value(i);
                if (area == null)
                {
                    w.EndObject();
                    continue;
                }

                w.Key("areaName").Value(area.areaName);
                w.Key("maxItems").Value(area.maxItems);
                w.Key("mandatoryItemName").Value(area.mandatoryItemName);
                w.Key("allowedItemNames");
                WriteStringList(w, area.allowedItemNames);

                w.Key("freeSpots").BeginArray();
                var spots = area.freeSpots;
                var spotCount = spots != null ? spots.Length : 0;
                for (var j = 0; j < spotCount; j++)
                {
                    w.Element();
                    WriteTransformRef(w, spots[j], includePosition: true);
                }
                w.EndArray();

                w.EndObject();
            }
            w.EndArray();

            w.EndObject();

            var fileName = $"config_{sm.Seed}_{DumpUtil.Timestamp()}_{call:D6}.json";
            DumpUtil.WriteJson(fileName, w, log);
        }

        // ------------------------------------------------------------------
        // placement_*.json — the ground truth
        // ------------------------------------------------------------------

        private static void DumpPlacement(SeedManager sm, int call, MelonLogger.Instance log)
        {
            var w = new JsonWriter();
            w.BeginObject();

            w.Key("dumpType").Value("placement");
            w.Key("callIndex").Value(call);
            w.Key("capturedAtUtc").Value(DateTime.UtcNow.ToString("o"));
            w.Key("seed").Value(sm.Seed);

            w.Key("items").BeginArray();
            var items = sm.allItems;
            var itemCount = items != null ? items.Count : 0;
            for (var i = 0; i < itemCount; i++)
            {
                var go = items[i];
                w.Element().BeginObject();
                w.Key("index").Value(i);
                if (go == null)
                {
                    w.Key("name").Null();
                }
                else
                {
                    w.Key("name").Value(go.name);
                    w.Key("instanceId").Value(go.GetInstanceID());

                    ItemSeedData isd;
                    try { isd = go.GetComponent<ItemSeedData>(); }
                    catch { isd = null; }
                    w.Key("itemName").Value(isd != null ? isd.itemName : null);

                    var t = go.transform;
                    var pos = t != null ? t.position : Vector3.zero;
                    w.Key("position").BeginObject();
                    w.Key("x").Value(pos.x);
                    w.Key("y").Value(pos.y);
                    w.Key("z").Value(pos.z);
                    w.EndObject();

                    w.Key("parent");
                    WriteTransformRef(w, t != null ? t.parent : null);

                    w.Key("activeSelf").Value(go.activeSelf);
                }
                w.EndObject();
            }
            w.EndArray();

            w.EndObject();

            var fileName = $"placement_{sm.Seed}_{DumpUtil.Timestamp()}_{call:D6}.json";
            DumpUtil.WriteJson(fileName, w, log);
        }

        // ------------------------------------------------------------------
        // Shared helpers
        // ------------------------------------------------------------------

        private static void WriteStringList(JsonWriter w, Il2CppSystem.Collections.Generic.List<string> list)
        {
            w.BeginArray();
            if (list != null)
            {
                var n = list.Count;
                for (var i = 0; i < n; i++)
                {
                    w.Element().Value(list[i]);
                }
            }
            w.EndArray();
        }

        /// <summary>Name + full hierarchy path + instance id, so spawn points can be
        /// matched unambiguously between the config dump and the placement dump.
        /// When <paramref name="includePosition"/> is set, also emits the
        /// Transform's world position (transform.position, not localPosition)
        /// so a placement's final position can be joined back to the named
        /// spot it landed on.</summary>
        private static void WriteTransformRef(JsonWriter w, Transform t, bool includePosition = false)
        {
            w.BeginObject();
            if (t == null)
            {
                w.Key("name").Null();
                w.Key("path").Null();
                w.Key("instanceId").Null();
                if (includePosition) w.Key("position").Null();
            }
            else
            {
                w.Key("name").Value(t.name);
                w.Key("path").Value(DumpUtil.FullPath(t));
                w.Key("instanceId").Value(t.GetInstanceID());
                if (includePosition)
                {
                    var pos = t.position;
                    w.Key("position").BeginObject();
                    w.Key("x").Value(pos.x);
                    w.Key("y").Value(pos.y);
                    w.Key("z").Value(pos.z);
                    w.EndObject();
                }
            }
            w.EndObject();
        }
    }
}
