import OpenAI from "openai";

function required(value, name) {
  if (typeof value === "string" && value.trim().length > 0) {
    return value.trim();
  }
  throw new Error(`Missing required value: ${name}`);
}

async function main() {
  const baseURL = required(process.env.BASE_URL ?? "http://127.0.0.1:8787/v1", "BASE_URL");
  const apiKey = process.env.OPENAI_API_KEY ?? "placeholder";
  const model = process.env.MODEL ?? "gpt-5";

  const client = new OpenAI({ baseURL, apiKey });

  console.log(`[js] base_url=${baseURL} model=${model}`);

  const models = await client.models.list();
  const modelCount = Array.isArray(models.data) ? models.data.length : 0;
  if (modelCount <= 0) {
    throw new Error("models.list returned no models");
  }
  console.log(`[js] models.list ok (${modelCount} models)`);

  const response = await client.responses.create({
    model,
    instructions: "You are a helpful assistant.",
    input: "Reply exactly: js responses non-stream ok",
  });
  if (typeof response.output_text !== "string" || response.output_text.trim().length === 0) {
    throw new Error("responses.create non-stream returned empty output_text");
  }
  console.log("[js] responses.create non-stream ok");

  const stream = await client.responses.create({
    model,
    instructions: "You are a helpful assistant.",
    input: "Reply with five words about streaming",
    stream: true,
  });
  let streamEvents = 0;
  let streamText = "";
  for await (const event of stream) {
    streamEvents += 1;
    if (event?.type === "response.output_text.delta" && typeof event.delta === "string") {
      streamText += event.delta;
    }
  }
  if (streamEvents === 0) {
    throw new Error("responses.create stream produced zero events");
  }
  if (streamText.trim().length === 0) {
    throw new Error("responses.create stream produced no text deltas");
  }
  console.log("[js] responses.create stream ok");

  console.log("[js] all compatibility checks passed (models + responses non-stream + responses stream)");
}

main().catch((err) => {
  console.error(`[js] FAIL: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
