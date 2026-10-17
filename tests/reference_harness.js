// Synthetic song for the reference analysis: 16 bars intro (kick + hats), 16 bars break (pad only),
// 32 bars drop (kick, bass, pad, hats). Prints the analysis as JSON; with a third argument it
// writes the song there as a 16 bit stereo WAV instead.
const { analyze, beatGrid, markerTempo } = require(process.argv[2]);
const rate = 22050, bpm = 128, spb = (rate * 60) / bpm;
const bars = 64, n = Math.round(bars * 4 * spb);
const s = new Float32Array(n);
let seed = 1;
const noise = () => ((seed = (seed * 16807) % 2147483647) / 2147483647) * 2 - 1;
for (let i = 0; i < n; i++) {
  const beat = i / spb, bar = Math.floor(beat / 4), t = (beat % 1) * (60 / bpm);
  const intro = bar < 16, brk = bar >= 16 && bar < 32, drop = bar >= 32;
  let v = 0;
  if (!brk) v += 0.8 * Math.exp(-t * 18) * Math.sin(2 * Math.PI * (50 + 80 * Math.exp(-t * 30)) * t); // kick on every beat
  if (drop) v += 0.25 * Math.sin(2 * Math.PI * 55 * (i / rate)) * (beat % 1 > 0.25 ? 1 : 0.2); // offbeat bass in A
  if (brk || drop) for (const f of [220, 261.63, 329.63]) v += 0.06 * Math.sin(2 * Math.PI * f * (i / rate)); // A minor pad
  if (!brk) v += 0.05 * noise() * Math.exp(-((beat * 2) % 1) * 40); // hats on 8ths
  s[i] = v;
}
if (process.argv[3]) {
  const b = Buffer.alloc(44 + n * 4);
  b.write("RIFF", 0); b.writeUInt32LE(36 + n * 4, 4); b.write("WAVEfmt ", 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(2, 22); b.writeUInt32LE(rate, 24);
  b.writeUInt32LE(rate * 4, 28); b.writeUInt16LE(4, 32); b.writeUInt16LE(16, 34); b.write("data", 36); b.writeUInt32LE(n * 4, 40);
  for (let i = 0; i < n; i++) {
    const v = Math.round(Math.max(-1, Math.min(1, s[i])) * 32767);
    b.writeInt16LE(v, 44 + i * 4); b.writeInt16LE(v, 46 + i * 4);
  }
  require("fs").writeFileSync(process.argv[3], b);
  process.exit(0);
}
const markers = [{ beat_time: 0, sample_time: 0 }, { beat_time: 256, sample_time: (256 * 60) / bpm }];
const grid = beatGrid(markers, 0, bars * 4, rate);
const result = analyze(s, rate, grid, { genre: process.env.GENRE || "auto", bpm, sensitivity: JSON.parse(process.env.SENSITIVITY || "{}") });
console.log(JSON.stringify({ tempo: markerTempo(markers), grid: [grid[0], grid[4], grid[256]], ...result }));
