export class FakeSocket extends EventTarget {
  readyState = 0; // CONNECTING
  sent: string[] = [];
  closed = false;

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.closed = true;
    this.readyState = 3; // CLOSED
    this.dispatchEvent(new Event("close"));
  }

  open() {
    this.readyState = 1; // OPEN
    this.dispatchEvent(new Event("open"));
  }

  receive(payload: unknown) {
    this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(payload) }));
  }
}
