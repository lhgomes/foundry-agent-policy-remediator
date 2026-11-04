import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, call, patch
from governance import Governance, Settings, parse_component, serving_versions, POLICY_API

PROJECT = '/subscriptions/2448105d-1c07-4767-a7b9-a06539a1169d/resourceGroups/rg-gg-dev-627df326/providers/Microsoft.CognitiveServices/accounts/ais-gg-bax5zaphblyxc/projects/gg-counsellor'
SUBSCRIPTION = PROJECT.split('/')[2]
OTHER_PROJECT = PROJECT.replace('ais-gg-bax5zaphblyxc', 'other-account').replace('gg-counsellor', 'other-project')
POLICY_DEFINITION = '/providers/Microsoft.Authorization/policyDefinitions/e7352ad2-6c86-4a07-9573-47f67b557ba8'
OTHER_POLICY = f'/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/policyDefinitions/custom-policy'


def finding(project=PROJECT, version='3', **overrides):
    account, project_name = project.split('/')[-3], project.split('/')[-1]
    return {
        'complianceState': 'NonCompliant',
        'policyDefinitionId': POLICY_DEFINITION,
        'resourceId': project,
        'componentId': f'accounts/{account}/projects/{project_name}/agents/example/versions/{version}',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        **overrides,
    }


def serving_agent():
    return {
        'name': 'example',
        'versions': {'latest': {'version': '3'}},
        'agent_endpoint': {'version_selector': {'version_selection_rules': [
            {'type': 'FixedRatio', 'agent_version': '@latest', 'traffic_percentage': 100},
        ]}},
    }


class Tests(unittest.TestCase):
    def test_component(self):
        self.assertEqual(parse_component('accounts/ais-gg-bax5zaphblyxc/projects/gg-counsellor/agents/example/versions/2', 'ais-gg-bax5zaphblyxc', 'gg-counsellor'), ('example', '2'))
        self.assertIsNone(parse_component('accounts/a/projects/p/toolboxes/t/versions/1', 'a', 'p'))

    def test_serving(self):
        agent = {'versions': {'latest': {'version': '3'}}, 'agent_endpoint': {'version_selector': {'version_selection_rules': [{'type': 'FixedRatio', 'agent_version': '@latest', 'traffic_percentage': 100}]}}}
        self.assertEqual(serving_versions(agent), {'3'})
        self.assertIsNone(serving_versions({}))

    def test_run_dry_and_scope(self):
        settings = Settings(SUBSCRIPTION, '', ('governance@example.org',), policy_definition_ids=(POLICY_DEFINITION,))
        gov = Governance(settings, Mock())
        gov.components = Mock(return_value=[finding(), finding(policyDefinitionId='/other')])
        gov.agent = Mock(return_value=serving_agent())
        gov.disable = Mock()
        gov.notify = Mock()
        result = gov.run()
        self.assertEqual(result['dry_run'], 1)
        gov.agent.assert_called_once_with(PROJECT.lower(), 'example')
        gov.disable.assert_not_called()
        gov.notify.assert_not_called()

    def test_subscription_settings(self):
        with patch.dict(os.environ, {
            'SUBSCRIPTION_ID': f' {SUBSCRIPTION} ',
            'POLICY_DEFINITION_IDS': json.dumps([POLICY_DEFINITION]),
        }, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.subscription_id, SUBSCRIPTION)
        self.assertTrue(settings.dry_run)
        self.assertEqual(settings.policy_definition_ids, (POLICY_DEFINITION,))
        for value in ('', PROJECT, 'not-a-guid', SUBSCRIPTION + '/'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'SUBSCRIPTION_ID'):
                Settings(value, '', (), policy_definition_ids=(POLICY_DEFINITION,))
        with patch.dict(os.environ, {
            'POLICY_DEFINITION_IDS': json.dumps([POLICY_DEFINITION]),
        }, clear=True), self.assertRaises(KeyError):
            Settings.from_env()

    def test_policy_configuration_is_required(self):
        with patch.dict(os.environ, {'SUBSCRIPTION_ID': SUBSCRIPTION}, clear=True):
            with self.assertRaisesRegex(ValueError, 'POLICY_DEFINITION_IDS is required'):
                Settings.from_env()
        with self.assertRaises(TypeError):
            Settings(SUBSCRIPTION, '', ())

    def test_sender_required_before_enforcement(self):
        for value in (None, '', '   '):
            with self.subTest(sender=value):
                values = {
                    'SUBSCRIPTION_ID': SUBSCRIPTION,
                    'POLICY_DEFINITION_IDS': json.dumps([POLICY_DEFINITION]),
                    'GOVERNANCE_RECIPIENTS': 'governance@example.org',
                    'DRY_RUN': 'false',
                }
                if value is not None:
                    values['SENDER_MAILBOX'] = value
                with patch.dict(os.environ, values, clear=True):
                    with self.assertRaisesRegex(ValueError, 'SENDER_MAILBOX is required when DRY_RUN=false'):
                        Settings.from_env()
                with self.assertRaisesRegex(ValueError, 'SENDER_MAILBOX'):
                    Settings(SUBSCRIPTION, value or '', (), policy_definition_ids=(POLICY_DEFINITION,), dry_run=False)
        settings = Settings(SUBSCRIPTION, 'sender@example.org', (), policy_definition_ids=(POLICY_DEFINITION,), dry_run=False)
        self.assertFalse(settings.dry_run)

    def test_policy_settings(self):
        management_policy = '/providers/Microsoft.Management/managementGroups/example/providers/Microsoft.Authorization/policyDefinitions/custom-policy'
        definitions = [POLICY_DEFINITION, f' {OTHER_POLICY.upper()} ', management_policy]
        with patch.dict(os.environ, {
            'SUBSCRIPTION_ID': SUBSCRIPTION,
            'POLICY_DEFINITION_IDS': json.dumps(definitions),
        }, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.policy_definition_ids, tuple(definitions))

    def test_invalid_policy_settings(self):
        for value in [
            '', 'not-json', POLICY_DEFINITION, '{}', 'null', 'true', '123',
            json.dumps(POLICY_DEFINITION), '[]', '[""]', '["   "]', '[null]', '[123]',
            '[{}]', '[[]]', '["e7352ad2-6c86-4a07-9573-47f67b557ba8"]',
            json.dumps([POLICY_DEFINITION, '/invalid']),
            json.dumps([OTHER_POLICY.replace('policyDefinitions', 'policyAssignments')]),
            json.dumps([OTHER_POLICY.replace('policyDefinitions', 'policySetDefinitions')]),
        ]:
            with self.subTest(value=value), patch.dict(os.environ, {
                'SUBSCRIPTION_ID': SUBSCRIPTION, 'POLICY_DEFINITION_IDS': value,
            }, clear=True), self.assertRaisesRegex(ValueError, 'POLICY_DEFINITION_IDS'):
                Settings.from_env()
        with self.assertRaisesRegex(ValueError, 'POLICY_DEFINITION_IDS'):
            Settings(SUBSCRIPTION, '', (), policy_definition_ids=())

    def test_multiple_policy_selection_and_override(self):
        for definitions, expected_agents in [
            ((POLICY_DEFINITION, f' {OTHER_POLICY.upper()} '), ['example', 'second']),
            ((OTHER_POLICY,), ['second']),
        ]:
            with self.subTest(definitions=definitions):
                settings = Settings(SUBSCRIPTION, '', ('governance@example.org',), policy_definition_ids=definitions)
                gov = Governance(settings, Mock())
                second = finding(policyDefinitionId=OTHER_POLICY)
                second['componentId'] = second['componentId'].replace('/agents/example/', '/agents/second/')
                gov.components = Mock(return_value=[
                    finding(), second,
                    finding(policyDefinitionId='/unselected'),
                    finding(policyDefinitionId=None),
                    finding(policyDefinitionId=123),
                    finding(policyDefinitionId=OTHER_POLICY, complianceState='Compliant'),
                ])
                gov.agent = Mock(return_value=serving_agent())
                gov.disable = Mock()
                gov.notify = Mock()
                result = gov.run()
                self.assertEqual(result['components'], len(expected_agents))
                self.assertEqual(result['dry_run'], len(expected_agents))
                self.assertEqual(result['errors'], 0)
                self.assertEqual(gov.agent.call_args_list, [
                    call(PROJECT.lower(), name) for name in expected_agents
                ])
                gov.disable.assert_not_called()
                gov.notify.assert_not_called()

    def test_multiple_policies_disable_and_notify_agent_once(self):
        settings = Settings(SUBSCRIPTION, 'sender@example.org', ('governance@example.org',),
                            dry_run=False, policy_definition_ids=(POLICY_DEFINITION, OTHER_POLICY))
        gov = Governance(settings, Mock())
        gov.components = Mock(return_value=[
            finding(),
            finding(policyDefinitionId=OTHER_POLICY, version='4'),
            finding(policyDefinitionId=OTHER_POLICY.upper()),
        ])
        agent = serving_agent()
        agent['agent_endpoint']['version_selector']['version_selection_rules'] = [
            {'type': 'FixedRatio', 'agent_version': version, 'traffic_percentage': 50}
            for version in ('3', '4')
        ]
        gov.agent = Mock(return_value=agent)
        gov.disable = Mock()
        gov.notify = Mock()
        result = gov.run()
        self.assertEqual(result, {'components': 3, 'agents': 1, 'disabled': 1, 'dry_run': 0, 'skipped': 0, 'errors': 0})
        gov.agent.assert_called_once_with(PROJECT.lower(), 'example')
        gov.disable.assert_called_once_with(PROJECT.lower(), 'example')
        gov.notify.assert_called_once_with(PROJECT.lower(), 'example', {'3', '4'}, None,
                                           'disabled for policy noncompliance')

    def test_subscription_query_pagination(self):
        gov = Governance(Settings(SUBSCRIPTION, '', (), policy_definition_ids=(POLICY_DEFINITION,)), Mock())
        url = (f'https://management.azure.com/subscriptions/{SUBSCRIPTION}'
               f'/providers/Microsoft.PolicyInsights/componentPolicyStates/latest/queryResults?api-version={POLICY_API}')
        next_url = url + '&$skiptoken=next'
        gov.call = Mock(side_effect=[
            {'value': [finding()], '@odata.nextLink': next_url},
            {'value': [finding(OTHER_PROJECT)]},
        ])
        rows = gov.components()
        self.assertEqual(len(rows), 2)
        self.assertEqual(gov.call.call_args_list, [
            call('POST', url, 'https://management.azure.com/.default', payload={}),
            call('POST', next_url, 'https://management.azure.com/.default', payload={}),
        ])

    def test_same_agent_name_in_different_projects(self):
        gov = Governance(Settings(SUBSCRIPTION, 'sender@example.org', ('governance@example.org',),
                                  policy_definition_ids=(POLICY_DEFINITION,), dry_run=False), Mock())
        gov.components = Mock(return_value=[
            finding(),
            finding(PROJECT.upper()),
            finding(OTHER_PROJECT),
        ])
        gov.agent = Mock(return_value=serving_agent())
        gov.disable = Mock()
        gov.notify = Mock()
        result = gov.run()
        self.assertEqual(result, {'components': 3, 'agents': 2, 'disabled': 2, 'dry_run': 0, 'skipped': 0, 'errors': 0})
        expected = [call(PROJECT.lower(), 'example'), call(OTHER_PROJECT.lower(), 'example')]
        self.assertEqual(gov.agent.call_args_list, expected)
        self.assertEqual(gov.disable.call_args_list, expected)
        self.assertEqual(gov.notify.call_args_list, [
            call(project.lower(), 'example', {'3'}, None, 'disabled for policy noncompliance')
            for project in (PROJECT, OTHER_PROJECT)
        ])

    def test_invalid_or_mismatched_project_findings(self):
        gov = Governance(Settings(SUBSCRIPTION, '', ('governance@example.org',), policy_definition_ids=(POLICY_DEFINITION,)), Mock())
        gov.components = Mock(return_value=[
            finding(resourceId=None),
            finding(resourceId='/invalid'),
            finding(resourceId=PROJECT.replace(SUBSCRIPTION, '00000000-0000-0000-0000-000000000000')),
            finding(resourceId=OTHER_PROJECT),
            finding(componentId='accounts/ais-gg-bax5zaphblyxc/projects/gg-counsellor/toolboxes/t/versions/3'),
            finding(complianceState='Compliant'),
            finding(policyDefinitionId=None),
        ])
        gov.agent = Mock()
        result = gov.run()
        self.assertEqual(result['agents'], 0)
        self.assertEqual(result['skipped'], 3)
        gov.agent.assert_not_called()

    def test_project_endpoint_routing(self):
        gov = Governance(Settings(SUBSCRIPTION, '', (), policy_definition_ids=(POLICY_DEFINITION,)), Mock())
        gov.call = Mock(return_value={})
        for project, endpoint in [
            (PROJECT, 'https://ais-gg-bax5zaphblyxc.services.ai.azure.com/api/projects/gg-counsellor'),
            (OTHER_PROJECT, 'https://other-account.services.ai.azure.com/api/projects/other-project'),
        ]:
            with self.subTest(project=project):
                gov.agent(project, 'example')
                gov.call.assert_called_with('GET', f'{endpoint}/agents/example?api-version=v1', 'https://ai.azure.com/.default')
                gov.disable(project, 'example')
                gov.call.assert_called_with('POST', f'{endpoint}/agents/example:disable?api-version=v1',
                                            'https://ai.azure.com/.default', expected=(200, 202, 204))
        with self.assertRaises(ValueError):
            gov.agent(PROJECT.replace(SUBSCRIPTION, '00000000-0000-0000-0000-000000000000'), 'example')

    def test_notification_identifies_project(self):
        gov = Governance(Settings(SUBSCRIPTION, 'sender@example.org', ('governance@example.org',),
                                  policy_definition_ids=(POLICY_DEFINITION,)), Mock())
        gov.call = Mock()
        gov.notify(OTHER_PROJECT, 'example', {'3'}, 'owner@example.org', 'disabled')
        payload = gov.call.call_args.kwargs['payload']
        self.assertIn(OTHER_PROJECT, payload['message']['body']['content'])
        self.assertEqual(payload['message']['toRecipients'], [
            {'emailAddress': {'address': 'governance@example.org'}},
            {'emailAddress': {'address': 'owner@example.org'}},
        ])

    def test_existing_safety_checks(self):
        for row, agent in [
            (finding(timestamp=None), serving_agent()),
            (finding(timestamp=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()), serving_agent()),
            (finding(timestamp=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()), serving_agent()),
            (finding(version='2'), serving_agent()),
            (finding(), {}),
            (finding(), {**serving_agent(), 'state': 'disabled'}),
        ]:
            with self.subTest(row=row, agent=agent):
                gov = Governance(Settings(SUBSCRIPTION, 'sender@example.org', ('governance@example.org',),
                                          policy_definition_ids=(POLICY_DEFINITION,), dry_run=False), Mock())
                gov.components = Mock(return_value=[row])
                gov.agent = Mock(return_value=agent)
                gov.disable = Mock()
                result = gov.run()
                self.assertEqual(result['skipped'], 1)
                gov.disable.assert_not_called()

    def test_project_failure_does_not_block_other_projects(self):
        gov = Governance(Settings(SUBSCRIPTION, '', ('governance@example.org',), policy_definition_ids=(POLICY_DEFINITION,)), Mock())
        gov.components = Mock(return_value=[finding(), finding(OTHER_PROJECT)])
        gov.agent = Mock(side_effect=[RuntimeError('Access denied'), serving_agent()])
        with self.assertLogs(level='ERROR'):
            result = gov.run()
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['dry_run'], 1)


if __name__ == '__main__':
    unittest.main()
