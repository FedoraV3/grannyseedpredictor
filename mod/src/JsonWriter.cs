using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace SeedDumper
{
    /// <summary>
    /// Minimal hand-rolled JSON writer.
    ///
    /// We deliberately do not reach for System.Text.Json (or any reflection
    /// based serializer): everything we serialize is read one field at a time
    /// off Il2Cpp interop objects (GameObject/Transform/PuzzleDef/...), there
    /// is no POCO graph to hand a serializer, and writing the handful of
    /// tokens by hand is far more predictable than fighting one to understand
    /// a domain it was never built for.
    ///
    /// Usage convention (this is what keeps commas correct):
    ///   - Call <see cref="Key"/> before every object member's value.
    ///   - Call <see cref="Element"/> before every bare array entry's value.
    ///   - <see cref="BeginObject"/>/<see cref="BeginArray"/>/<see cref="Value"/>
    ///     overloads/<see cref="Null"/> never insert a separator themselves —
    ///     the container (via Key/Element) already did that.
    /// </summary>
    internal sealed class JsonWriter
    {
        private readonly StringBuilder _sb = new StringBuilder(1 << 16);
        private readonly Stack<bool> _first = new Stack<bool>();

        public JsonWriter BeginObject()
        {
            _sb.Append('{');
            _first.Push(true);
            return this;
        }

        public JsonWriter EndObject()
        {
            _sb.Append('}');
            _first.Pop();
            return this;
        }

        public JsonWriter BeginArray()
        {
            _sb.Append('[');
            _first.Push(true);
            return this;
        }

        public JsonWriter EndArray()
        {
            _sb.Append(']');
            _first.Pop();
            return this;
        }

        /// <summary>Announce the next bare array element (inserts a leading comma if needed).</summary>
        public JsonWriter Element()
        {
            Separator();
            return this;
        }

        /// <summary>Announce an object member's key (inserts a leading comma if needed).</summary>
        public JsonWriter Key(string name)
        {
            Separator();
            WriteString(name);
            _sb.Append(':');
            return this;
        }

        private void Separator()
        {
            if (_first.Count == 0) return;
            if (_first.Peek())
            {
                _first.Pop();
                _first.Push(false);
            }
            else
            {
                _sb.Append(',');
            }
        }

        public JsonWriter Value(string s)
        {
            if (s == null) _sb.Append("null");
            else WriteString(s);
            return this;
        }

        public JsonWriter Value(int i)
        {
            _sb.Append(i.ToString(CultureInfo.InvariantCulture));
            return this;
        }

        public JsonWriter Value(long i)
        {
            _sb.Append(i.ToString(CultureInfo.InvariantCulture));
            return this;
        }

        public JsonWriter Value(float f)
        {
            if (float.IsNaN(f) || float.IsInfinity(f)) _sb.Append("null");
            else _sb.Append(f.ToString("R", CultureInfo.InvariantCulture));
            return this;
        }

        public JsonWriter Value(double d)
        {
            if (double.IsNaN(d) || double.IsInfinity(d)) _sb.Append("null");
            else _sb.Append(d.ToString("R", CultureInfo.InvariantCulture));
            return this;
        }

        public JsonWriter Value(bool b)
        {
            _sb.Append(b ? "true" : "false");
            return this;
        }

        public JsonWriter Null()
        {
            _sb.Append("null");
            return this;
        }

        private void WriteString(string s)
        {
            _sb.Append('"');
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"': _sb.Append("\\\""); break;
                    case '\\': _sb.Append("\\\\"); break;
                    case '\n': _sb.Append("\\n"); break;
                    case '\r': _sb.Append("\\r"); break;
                    case '\t': _sb.Append("\\t"); break;
                    case '\b': _sb.Append("\\b"); break;
                    case '\f': _sb.Append("\\f"); break;
                    default:
                        if (c < 0x20)
                        {
                            _sb.Append("\\u");
                            _sb.Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            _sb.Append(c);
                        }
                        break;
                }
            }
            _sb.Append('"');
        }

        public override string ToString() => _sb.ToString();
    }
}
