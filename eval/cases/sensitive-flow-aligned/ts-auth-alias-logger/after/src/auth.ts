const logger = console;

export function check(req: any): void {
  const token = req.token;
  const auth = token;
  logger.info(auth);
}
