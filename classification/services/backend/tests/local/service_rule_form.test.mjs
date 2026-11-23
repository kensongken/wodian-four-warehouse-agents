import test from 'node:test';
import assert from 'node:assert/strict';
import { buildServiceRules } from '../../src/product_engine/api/service_rule_form.mjs';

test('all-empty service row means no rule', () => {
  assert.deepEqual(buildServiceRules({
    serviceCode: ' ',
    serviceEligibility: '',
    serviceCustomerSummary: '',
  }), []);
});

test('complete service row becomes one trimmed rule', () => {
  assert.deepEqual(buildServiceRules({
    serviceCode: ' scan_identity ',
    serviceEligibility: ' 购买后可用 ',
    serviceCustomerSummary: ' 购买后可扫码进入身份与服务页面 ',
  }), [{
    service_code: 'scan_identity',
    eligibility: '购买后可用',
    customer_summary: '购买后可扫码进入身份与服务页面',
  }]);
});

test('partial service row is preserved so backend can return field guidance', () => {
  assert.deepEqual(buildServiceRules({
    serviceCode: 'scan_identity',
    serviceEligibility: '',
    serviceCustomerSummary: '',
  }), [{
    service_code: 'scan_identity',
    eligibility: '',
    customer_summary: '',
  }]);
});
