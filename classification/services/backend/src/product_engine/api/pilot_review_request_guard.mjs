export function createPilotReviewRequestGuard(onReset) {
  let activeRequestId = 0;

  return Object.freeze({
    begin(templateCode) {
      activeRequestId += 1;
      onReset(templateCode);
      return Object.freeze({ requestId: activeRequestId, templateCode });
    },
    isCurrent(ticket, currentTemplateCode) {
      return ticket.requestId === activeRequestId
        && ticket.templateCode === currentTemplateCode;
    },
  });
}
