import fs from "node:fs";

const [, , wsUrl, expressionInput] = process.argv;

if (!wsUrl || !expressionInput) {
  console.error("Usage: node devtools_eval.mjs <ws-url> <base64-expression|@file-path>");
  process.exit(1);
}

const expression =
  expressionInput.startsWith("@")
    ? fs.readFileSync(expressionInput.slice(1), "utf8")
    : Buffer.from(expressionInput, "base64").toString("utf8");
const ws = new WebSocket(wsUrl);

let id = 0;
const pending = new Map();

function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const msgId = ++id;
    pending.set(msgId, { resolve, reject });
    ws.send(JSON.stringify({ id: msgId, method, params }));
  });
}

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (!msg.id || !pending.has(msg.id)) {
    return;
  }

  const entry = pending.get(msg.id);
  pending.delete(msg.id);

  if (msg.error) {
    entry.reject(new Error(JSON.stringify(msg.error)));
    return;
  }

  entry.resolve(msg.result);
};

ws.onopen = async () => {
  try {
    await send("Runtime.enable");
    const result = await send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    console.log(JSON.stringify(result.result.value, null, 2));
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  } finally {
    ws.close();
  }
};
