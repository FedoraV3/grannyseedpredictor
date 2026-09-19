using System;
using System.IO;
using System.Text;
using MelonLoader;
using UnityEngine;

namespace SeedDumper
{
    internal static class DumpUtil
    {
        /// <summary>
        /// Fixed, absolute output location (never the game install). Kept as a
        /// literal rather than derived from the mod's own assembly location so
        /// it stays correct however the mod happens to be loaded.
        /// </summary>
        public static readonly string DumpDir = @"C:\Users\ir0n1c\grannyseedpredictor\dumps";

        public static void EnsureDumpDir()
        {
            Directory.CreateDirectory(DumpDir);
        }

        public static string Timestamp() => DateTime.Now.ToString("yyyyMMdd_HHmmss_fff");

        /// <summary>
        /// Full "/"-joined path from the scene root down to and including
        /// <paramref name="t"/>. Names alone can collide (e.g. duplicated
        /// prefabs); combined with GetInstanceID() this is enough to
        /// unambiguously match spawn points/objects between the config dump
        /// and the placement dump.
        /// </summary>
        public static string FullPath(Transform t)
        {
            if (t == null) return null;
            var name = t.name;
            for (var p = t.parent; p != null; p = p.parent)
                name = p.name + "/" + name;
            return name;
        }

        public static void WriteJson(string fileName, JsonWriter writer, MelonLogger.Instance log)
        {
            EnsureDumpDir();
            var path = Path.Combine(DumpDir, fileName);
            File.WriteAllText(path, writer.ToString(), new UTF8Encoding(false));
            log?.Msg("[SeedDumper] wrote " + path);
        }
    }
}
