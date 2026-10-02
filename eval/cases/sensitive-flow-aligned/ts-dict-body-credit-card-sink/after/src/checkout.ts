export function processPayment(body: any, res: any): void {
  const card = body['credit_card'];
  res.json({ card });
}
