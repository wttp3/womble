// Builds the Frida interaction model with the media intents (M2).
//   node skill/build-model.mjs <base-model.json> <out.json>
// Base: the current console model. SpikeIntent is removed; MEDIA_NAME values
// and synonyms come from the tracked library seed (public names only).
import { readFileSync, writeFileSync } from "node:fs";

const [base, out] = process.argv.slice(2);
const seed = JSON.parse(readFileSync(new URL("../../slice1/hub/media/library.seed.json", import.meta.url), "utf8"));
const model = JSON.parse(readFileSync(base, "utf8"));
const lm = model.interactionModel.languageModel;

const MEDIA = [
  { name: "PlayMediaIntent", slots: [{ name: "media", type: "MEDIA_NAME" }, { name: "room", type: "ROOM" }], samples: [
    "play {media}", "put on {media}", "play the {media}", "put on the {media}", "start {media}", "listen to {media}",
    "play {media} in the {room}", "play the {media} in the {room}", "put on {media} in the {room}",
    "play the podcast {media}", "play the {media} podcast", "play {media} podcast", "play the radio station {media}"] },
  { name: "PlayLatestMediaIntent", slots: [{ name: "media", type: "MEDIA_NAME" }], samples: [
    "play the latest {media}", "play the latest episode of {media}", "play the newest {media}",
    "play the new {media}", "play latest {media}", "play the latest {media} episode", "play the latest episode of the {media}"] },
  { name: "ResumeMediaIntent", slots: [{ name: "media", type: "MEDIA_NAME" }], samples: [
    "resume {media}", "resume the {media}", "continue {media}", "continue the {media}", "carry on with {media}",
    "carry on with the {media}"] },
  { name: "ListLibraryIntent", samples: [
    "what's in my library", "what is in my library", "what's in the library", "what is in the library",
    "what can you play", "what do you have to play", "list my library", "read my library", "what podcasts do you have",
    "what radio stations do you have"] },
];
const BUILTINS = ["AMAZON.PauseIntent", "AMAZON.ResumeIntent", "AMAZON.NextIntent", "AMAZON.PreviousIntent",
  "AMAZON.StartOverIntent", "AMAZON.RepeatIntent", "AMAZON.LoopOnIntent", "AMAZON.LoopOffIntent",
  "AMAZON.ShuffleOnIntent", "AMAZON.ShuffleOffIntent"];

const drop = new Set(["SpikeIntent", ...MEDIA.map((i) => i.name), ...BUILTINS]);
lm.intents = lm.intents.filter((i) => !drop.has(i.name));
lm.intents.push(...MEDIA, ...BUILTINS.map((name) => ({ name, samples: [] })));

const mediaValues = seed.entries.filter((e) => e.enabled !== false).map((e) => ({
  id: e.id,
  name: { value: e.name, synonyms: [...new Set((e.aliases || []).filter((a) => a.toLowerCase() !== e.name.toLowerCase()))] },
}));
const rooms = ["kitchen", "living room", "lounge", "bedroom", "office", "study", "bathroom", "dining room", "hall"]
  .map((r) => ({ name: { value: r } }));
lm.types = (lm.types || []).filter((t) => t.name !== "MEDIA_NAME" && t.name !== "ROOM");
lm.types.push({ name: "MEDIA_NAME", values: mediaValues }, { name: "ROOM", values: rooms });
writeFileSync(out, JSON.stringify(model, null, 2) + "\n");
console.log(`wrote ${out}: ${lm.intents.length} intents, ${mediaValues.length} media names`);
