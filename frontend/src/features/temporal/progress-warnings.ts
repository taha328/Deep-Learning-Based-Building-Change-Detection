const UNRELATED_BANDON_WARNING =
  "BANDON applied an MPS slide-window compatibility patch to the configured crop/stride.";

function isCaptureDateIntersectionWarning(message: string) {
  return message.includes("capture-date") && message.includes("regions within the AOI");
}

export function filterProgressWarnings(messages: string[]) {
  return messages.filter(
    (message) => message !== UNRELATED_BANDON_WARNING && !isCaptureDateIntersectionWarning(message),
  );
}
