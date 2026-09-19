using System;
using MelonLoader;

// MelonLoader resolves these two exactly as the compatibility check compares
// them (see MelonLoader/Latest.log): note the game's product name carries a
// colon ("Granny: Legacy") even though app.info spells it without one.
[assembly: MelonInfo(typeof(SeedDumper.SeedDumperMod), "SeedDumper", "1.1.0", "ir0n1c")]
[assembly: MelonGame("Omega Mega Gigal Intel", "Granny: Legacy")]

namespace SeedDumper
{
    /// <summary>
    /// Dumps the SeedManager's serialized configuration and the resulting item
    /// placement to JSON, so a from-scratch Python reimplementation of the
    /// randomizer can be validated against real game runs. See
    /// <see cref="SeedManagerPatch"/> for the actual capture logic; this class
    /// only wires up logging and the output directory.
    ///
    /// Read-only with respect to the game: it never writes back into the game
    /// install, never changes Seed/RandomizeSeed, and never blocks the
    /// original GeneratePlacement call. A dump failure is caught, logged, and
    /// otherwise ignored so it can never crash or desync the game.
    /// </summary>
    public sealed class SeedDumperMod : MelonMod
    {
        internal static MelonLogger.Instance Log { get; private set; }

        public override void OnInitializeMelon()
        {
            Log = LoggerInstance;
            try
            {
                DumpUtil.EnsureDumpDir();
                Log.Msg("SeedDumper v1.1.0 loaded.");
                Log.Msg("Patching SeedManager.GeneratePlacement (prefix = config dump, postfix = placement dump).");
                Log.Msg("Output directory: " + DumpUtil.DumpDir);
            }
            catch (Exception e)
            {
                Log.Error("SeedDumper failed to initialize: " + e);
            }
        }
    }
}
