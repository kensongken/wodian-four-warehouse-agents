export function buildServiceRules({
  serviceCode = '',
  serviceEligibility = '',
  serviceCustomerSummary = '',
} = {}) {
  const rule = {
    service_code: serviceCode.trim(),
    eligibility: serviceEligibility.trim(),
    customer_summary: serviceCustomerSummary.trim(),
  };
  return Object.values(rule).some(Boolean) ? [rule] : [];
}
