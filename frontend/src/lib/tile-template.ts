export function normalizeTileTemplatePlaceholders(template: string): string {
  return template.replace(/%7B/gi, "{").replace(/%7D/gi, "}");
}
