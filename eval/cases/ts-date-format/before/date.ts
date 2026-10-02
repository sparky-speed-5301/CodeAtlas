export function formatDate(value: Date, locale: string): string {
  return new Intl.DateTimeFormat(locale, { timeZone: "UTC" }).format(value);
}
