// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'upload_database.dart';

// ignore_for_file: type=lint
class $UploadItemsTable extends UploadItems
    with TableInfo<$UploadItemsTable, UploadItem> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $UploadItemsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _localAssetIdMeta = const VerificationMeta(
    'localAssetId',
  );
  @override
  late final GeneratedColumn<String> localAssetId = GeneratedColumn<String>(
    'local_asset_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _taskIdMeta = const VerificationMeta('taskId');
  @override
  late final GeneratedColumn<String> taskId = GeneratedColumn<String>(
    'task_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _bindingIdMeta = const VerificationMeta(
    'bindingId',
  );
  @override
  late final GeneratedColumn<String> bindingId = GeneratedColumn<String>(
    'binding_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _candidateIdMeta = const VerificationMeta(
    'candidateId',
  );
  @override
  late final GeneratedColumn<String> candidateId = GeneratedColumn<String>(
    'candidate_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _localRefJsonMeta = const VerificationMeta(
    'localRefJson',
  );
  @override
  late final GeneratedColumn<String> localRefJson = GeneratedColumn<String>(
    'local_ref_json',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _byteSizeMeta = const VerificationMeta(
    'byteSize',
  );
  @override
  late final GeneratedColumn<int> byteSize = GeneratedColumn<int>(
    'byte_size',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _sha256Meta = const VerificationMeta('sha256');
  @override
  late final GeneratedColumn<String> sha256 = GeneratedColumn<String>(
    'sha256',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _backendAssetIdMeta = const VerificationMeta(
    'backendAssetId',
  );
  @override
  late final GeneratedColumn<String> backendAssetId = GeneratedColumn<String>(
    'backend_asset_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _providerUploadIdMeta = const VerificationMeta(
    'providerUploadId',
  );
  @override
  late final GeneratedColumn<String> providerUploadId = GeneratedColumn<String>(
    'provider_upload_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _partSizeMeta = const VerificationMeta(
    'partSize',
  );
  @override
  late final GeneratedColumn<int> partSize = GeneratedColumn<int>(
    'part_size',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _retryCountMeta = const VerificationMeta(
    'retryCount',
  );
  @override
  late final GeneratedColumn<int> retryCount = GeneratedColumn<int>(
    'retry_count',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultValue: const Constant(0),
  );
  static const VerificationMeta _nextAttemptAtMeta = const VerificationMeta(
    'nextAttemptAt',
  );
  @override
  late final GeneratedColumn<DateTime> nextAttemptAt =
      GeneratedColumn<DateTime>(
        'next_attempt_at',
        aliasedName,
        true,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _stateMeta = const VerificationMeta('state');
  @override
  late final GeneratedColumn<String> state = GeneratedColumn<String>(
    'state',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _lastErrorCodeMeta = const VerificationMeta(
    'lastErrorCode',
  );
  @override
  late final GeneratedColumn<String> lastErrorCode = GeneratedColumn<String>(
    'last_error_code',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _lastErrorMessageMeta = const VerificationMeta(
    'lastErrorMessage',
  );
  @override
  late final GeneratedColumn<String> lastErrorMessage = GeneratedColumn<String>(
    'last_error_message',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _leaseOwnerMeta = const VerificationMeta(
    'leaseOwner',
  );
  @override
  late final GeneratedColumn<String> leaseOwner = GeneratedColumn<String>(
    'lease_owner',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _leaseExpiresAtMeta = const VerificationMeta(
    'leaseExpiresAt',
  );
  @override
  late final GeneratedColumn<DateTime> leaseExpiresAt =
      GeneratedColumn<DateTime>(
        'lease_expires_at',
        aliasedName,
        true,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _leaseGenerationMeta = const VerificationMeta(
    'leaseGeneration',
  );
  @override
  late final GeneratedColumn<int> leaseGeneration = GeneratedColumn<int>(
    'lease_generation',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultValue: const Constant(0),
  );
  static const VerificationMeta _policyGenerationMeta = const VerificationMeta(
    'policyGeneration',
  );
  @override
  late final GeneratedColumn<int> policyGeneration = GeneratedColumn<int>(
    'policy_generation',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultValue: const Constant(0),
  );
  static const VerificationMeta _serverConfirmedMeta = const VerificationMeta(
    'serverConfirmed',
  );
  @override
  late final GeneratedColumn<bool> serverConfirmed = GeneratedColumn<bool>(
    'server_confirmed',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("server_confirmed" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _terminalSummaryAcknowledgedMeta =
      const VerificationMeta('terminalSummaryAcknowledged');
  @override
  late final GeneratedColumn<bool> terminalSummaryAcknowledged =
      GeneratedColumn<bool>(
        'terminal_summary_acknowledged',
        aliasedName,
        false,
        type: DriftSqlType.bool,
        requiredDuringInsert: false,
        defaultConstraints: GeneratedColumn.constraintIsAlways(
          'CHECK ("terminal_summary_acknowledged" IN (0, 1))',
        ),
        defaultValue: const Constant(false),
      );
  static const VerificationMeta _retryRequestedMeta = const VerificationMeta(
    'retryRequested',
  );
  @override
  late final GeneratedColumn<bool> retryRequested = GeneratedColumn<bool>(
    'retry_requested',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("retry_requested" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _localRejectedMeta = const VerificationMeta(
    'localRejected',
  );
  @override
  late final GeneratedColumn<bool> localRejected = GeneratedColumn<bool>(
    'local_rejected',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("local_rejected" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _pausedFromStateMeta = const VerificationMeta(
    'pausedFromState',
  );
  @override
  late final GeneratedColumn<String> pausedFromState = GeneratedColumn<String>(
    'paused_from_state',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _failureReportRetryCountMeta =
      const VerificationMeta('failureReportRetryCount');
  @override
  late final GeneratedColumn<int> failureReportRetryCount =
      GeneratedColumn<int>(
        'failure_report_retry_count',
        aliasedName,
        false,
        type: DriftSqlType.int,
        requiredDuringInsert: false,
        defaultValue: const Constant(0),
      );
  static const VerificationMeta _failureReportNextAttemptAtMeta =
      const VerificationMeta('failureReportNextAttemptAt');
  @override
  late final GeneratedColumn<DateTime> failureReportNextAttemptAt =
      GeneratedColumn<DateTime>(
        'failure_report_next_attempt_at',
        aliasedName,
        true,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
      );
  @override
  List<GeneratedColumn> get $columns => [
    localAssetId,
    taskId,
    bindingId,
    candidateId,
    localRefJson,
    byteSize,
    sha256,
    backendAssetId,
    providerUploadId,
    partSize,
    retryCount,
    nextAttemptAt,
    state,
    lastErrorCode,
    lastErrorMessage,
    leaseOwner,
    leaseExpiresAt,
    leaseGeneration,
    policyGeneration,
    serverConfirmed,
    terminalSummaryAcknowledged,
    retryRequested,
    localRejected,
    pausedFromState,
    failureReportRetryCount,
    failureReportNextAttemptAt,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'upload_items';
  @override
  VerificationContext validateIntegrity(
    Insertable<UploadItem> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('local_asset_id')) {
      context.handle(
        _localAssetIdMeta,
        localAssetId.isAcceptableOrUnknown(
          data['local_asset_id']!,
          _localAssetIdMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_localAssetIdMeta);
    }
    if (data.containsKey('task_id')) {
      context.handle(
        _taskIdMeta,
        taskId.isAcceptableOrUnknown(data['task_id']!, _taskIdMeta),
      );
    } else if (isInserting) {
      context.missing(_taskIdMeta);
    }
    if (data.containsKey('binding_id')) {
      context.handle(
        _bindingIdMeta,
        bindingId.isAcceptableOrUnknown(data['binding_id']!, _bindingIdMeta),
      );
    }
    if (data.containsKey('candidate_id')) {
      context.handle(
        _candidateIdMeta,
        candidateId.isAcceptableOrUnknown(
          data['candidate_id']!,
          _candidateIdMeta,
        ),
      );
    }
    if (data.containsKey('local_ref_json')) {
      context.handle(
        _localRefJsonMeta,
        localRefJson.isAcceptableOrUnknown(
          data['local_ref_json']!,
          _localRefJsonMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_localRefJsonMeta);
    }
    if (data.containsKey('byte_size')) {
      context.handle(
        _byteSizeMeta,
        byteSize.isAcceptableOrUnknown(data['byte_size']!, _byteSizeMeta),
      );
    } else if (isInserting) {
      context.missing(_byteSizeMeta);
    }
    if (data.containsKey('sha256')) {
      context.handle(
        _sha256Meta,
        sha256.isAcceptableOrUnknown(data['sha256']!, _sha256Meta),
      );
    }
    if (data.containsKey('backend_asset_id')) {
      context.handle(
        _backendAssetIdMeta,
        backendAssetId.isAcceptableOrUnknown(
          data['backend_asset_id']!,
          _backendAssetIdMeta,
        ),
      );
    }
    if (data.containsKey('provider_upload_id')) {
      context.handle(
        _providerUploadIdMeta,
        providerUploadId.isAcceptableOrUnknown(
          data['provider_upload_id']!,
          _providerUploadIdMeta,
        ),
      );
    }
    if (data.containsKey('part_size')) {
      context.handle(
        _partSizeMeta,
        partSize.isAcceptableOrUnknown(data['part_size']!, _partSizeMeta),
      );
    } else if (isInserting) {
      context.missing(_partSizeMeta);
    }
    if (data.containsKey('retry_count')) {
      context.handle(
        _retryCountMeta,
        retryCount.isAcceptableOrUnknown(data['retry_count']!, _retryCountMeta),
      );
    }
    if (data.containsKey('next_attempt_at')) {
      context.handle(
        _nextAttemptAtMeta,
        nextAttemptAt.isAcceptableOrUnknown(
          data['next_attempt_at']!,
          _nextAttemptAtMeta,
        ),
      );
    }
    if (data.containsKey('state')) {
      context.handle(
        _stateMeta,
        state.isAcceptableOrUnknown(data['state']!, _stateMeta),
      );
    } else if (isInserting) {
      context.missing(_stateMeta);
    }
    if (data.containsKey('last_error_code')) {
      context.handle(
        _lastErrorCodeMeta,
        lastErrorCode.isAcceptableOrUnknown(
          data['last_error_code']!,
          _lastErrorCodeMeta,
        ),
      );
    }
    if (data.containsKey('last_error_message')) {
      context.handle(
        _lastErrorMessageMeta,
        lastErrorMessage.isAcceptableOrUnknown(
          data['last_error_message']!,
          _lastErrorMessageMeta,
        ),
      );
    }
    if (data.containsKey('lease_owner')) {
      context.handle(
        _leaseOwnerMeta,
        leaseOwner.isAcceptableOrUnknown(data['lease_owner']!, _leaseOwnerMeta),
      );
    }
    if (data.containsKey('lease_expires_at')) {
      context.handle(
        _leaseExpiresAtMeta,
        leaseExpiresAt.isAcceptableOrUnknown(
          data['lease_expires_at']!,
          _leaseExpiresAtMeta,
        ),
      );
    }
    if (data.containsKey('lease_generation')) {
      context.handle(
        _leaseGenerationMeta,
        leaseGeneration.isAcceptableOrUnknown(
          data['lease_generation']!,
          _leaseGenerationMeta,
        ),
      );
    }
    if (data.containsKey('policy_generation')) {
      context.handle(
        _policyGenerationMeta,
        policyGeneration.isAcceptableOrUnknown(
          data['policy_generation']!,
          _policyGenerationMeta,
        ),
      );
    }
    if (data.containsKey('server_confirmed')) {
      context.handle(
        _serverConfirmedMeta,
        serverConfirmed.isAcceptableOrUnknown(
          data['server_confirmed']!,
          _serverConfirmedMeta,
        ),
      );
    }
    if (data.containsKey('terminal_summary_acknowledged')) {
      context.handle(
        _terminalSummaryAcknowledgedMeta,
        terminalSummaryAcknowledged.isAcceptableOrUnknown(
          data['terminal_summary_acknowledged']!,
          _terminalSummaryAcknowledgedMeta,
        ),
      );
    }
    if (data.containsKey('retry_requested')) {
      context.handle(
        _retryRequestedMeta,
        retryRequested.isAcceptableOrUnknown(
          data['retry_requested']!,
          _retryRequestedMeta,
        ),
      );
    }
    if (data.containsKey('local_rejected')) {
      context.handle(
        _localRejectedMeta,
        localRejected.isAcceptableOrUnknown(
          data['local_rejected']!,
          _localRejectedMeta,
        ),
      );
    }
    if (data.containsKey('paused_from_state')) {
      context.handle(
        _pausedFromStateMeta,
        pausedFromState.isAcceptableOrUnknown(
          data['paused_from_state']!,
          _pausedFromStateMeta,
        ),
      );
    }
    if (data.containsKey('failure_report_retry_count')) {
      context.handle(
        _failureReportRetryCountMeta,
        failureReportRetryCount.isAcceptableOrUnknown(
          data['failure_report_retry_count']!,
          _failureReportRetryCountMeta,
        ),
      );
    }
    if (data.containsKey('failure_report_next_attempt_at')) {
      context.handle(
        _failureReportNextAttemptAtMeta,
        failureReportNextAttemptAt.isAcceptableOrUnknown(
          data['failure_report_next_attempt_at']!,
          _failureReportNextAttemptAtMeta,
        ),
      );
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {localAssetId};
  @override
  UploadItem map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return UploadItem(
      localAssetId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}local_asset_id'],
      )!,
      taskId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}task_id'],
      )!,
      bindingId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}binding_id'],
      ),
      candidateId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}candidate_id'],
      ),
      localRefJson: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}local_ref_json'],
      )!,
      byteSize: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}byte_size'],
      )!,
      sha256: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}sha256'],
      ),
      backendAssetId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}backend_asset_id'],
      ),
      providerUploadId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}provider_upload_id'],
      ),
      partSize: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}part_size'],
      )!,
      retryCount: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}retry_count'],
      )!,
      nextAttemptAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}next_attempt_at'],
      ),
      state: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}state'],
      )!,
      lastErrorCode: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}last_error_code'],
      ),
      lastErrorMessage: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}last_error_message'],
      ),
      leaseOwner: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}lease_owner'],
      ),
      leaseExpiresAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}lease_expires_at'],
      ),
      leaseGeneration: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}lease_generation'],
      )!,
      policyGeneration: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}policy_generation'],
      )!,
      serverConfirmed: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}server_confirmed'],
      )!,
      terminalSummaryAcknowledged: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}terminal_summary_acknowledged'],
      )!,
      retryRequested: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}retry_requested'],
      )!,
      localRejected: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}local_rejected'],
      )!,
      pausedFromState: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}paused_from_state'],
      ),
      failureReportRetryCount: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}failure_report_retry_count'],
      )!,
      failureReportNextAttemptAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}failure_report_next_attempt_at'],
      ),
    );
  }

  @override
  $UploadItemsTable createAlias(String alias) {
    return $UploadItemsTable(attachedDatabase, alias);
  }
}

class UploadItem extends DataClass implements Insertable<UploadItem> {
  final String localAssetId;
  final String taskId;
  final String? bindingId;
  final String? candidateId;
  final String localRefJson;
  final int byteSize;
  final String? sha256;
  final String? backendAssetId;
  final String? providerUploadId;
  final int partSize;
  final int retryCount;
  final DateTime? nextAttemptAt;
  final String state;
  final String? lastErrorCode;
  final String? lastErrorMessage;
  final String? leaseOwner;
  final DateTime? leaseExpiresAt;
  final int leaseGeneration;
  final int policyGeneration;
  final bool serverConfirmed;
  final bool terminalSummaryAcknowledged;
  final bool retryRequested;
  final bool localRejected;
  final String? pausedFromState;
  final int failureReportRetryCount;
  final DateTime? failureReportNextAttemptAt;
  const UploadItem({
    required this.localAssetId,
    required this.taskId,
    this.bindingId,
    this.candidateId,
    required this.localRefJson,
    required this.byteSize,
    this.sha256,
    this.backendAssetId,
    this.providerUploadId,
    required this.partSize,
    required this.retryCount,
    this.nextAttemptAt,
    required this.state,
    this.lastErrorCode,
    this.lastErrorMessage,
    this.leaseOwner,
    this.leaseExpiresAt,
    required this.leaseGeneration,
    required this.policyGeneration,
    required this.serverConfirmed,
    required this.terminalSummaryAcknowledged,
    required this.retryRequested,
    required this.localRejected,
    this.pausedFromState,
    required this.failureReportRetryCount,
    this.failureReportNextAttemptAt,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['local_asset_id'] = Variable<String>(localAssetId);
    map['task_id'] = Variable<String>(taskId);
    if (!nullToAbsent || bindingId != null) {
      map['binding_id'] = Variable<String>(bindingId);
    }
    if (!nullToAbsent || candidateId != null) {
      map['candidate_id'] = Variable<String>(candidateId);
    }
    map['local_ref_json'] = Variable<String>(localRefJson);
    map['byte_size'] = Variable<int>(byteSize);
    if (!nullToAbsent || sha256 != null) {
      map['sha256'] = Variable<String>(sha256);
    }
    if (!nullToAbsent || backendAssetId != null) {
      map['backend_asset_id'] = Variable<String>(backendAssetId);
    }
    if (!nullToAbsent || providerUploadId != null) {
      map['provider_upload_id'] = Variable<String>(providerUploadId);
    }
    map['part_size'] = Variable<int>(partSize);
    map['retry_count'] = Variable<int>(retryCount);
    if (!nullToAbsent || nextAttemptAt != null) {
      map['next_attempt_at'] = Variable<DateTime>(nextAttemptAt);
    }
    map['state'] = Variable<String>(state);
    if (!nullToAbsent || lastErrorCode != null) {
      map['last_error_code'] = Variable<String>(lastErrorCode);
    }
    if (!nullToAbsent || lastErrorMessage != null) {
      map['last_error_message'] = Variable<String>(lastErrorMessage);
    }
    if (!nullToAbsent || leaseOwner != null) {
      map['lease_owner'] = Variable<String>(leaseOwner);
    }
    if (!nullToAbsent || leaseExpiresAt != null) {
      map['lease_expires_at'] = Variable<DateTime>(leaseExpiresAt);
    }
    map['lease_generation'] = Variable<int>(leaseGeneration);
    map['policy_generation'] = Variable<int>(policyGeneration);
    map['server_confirmed'] = Variable<bool>(serverConfirmed);
    map['terminal_summary_acknowledged'] = Variable<bool>(
      terminalSummaryAcknowledged,
    );
    map['retry_requested'] = Variable<bool>(retryRequested);
    map['local_rejected'] = Variable<bool>(localRejected);
    if (!nullToAbsent || pausedFromState != null) {
      map['paused_from_state'] = Variable<String>(pausedFromState);
    }
    map['failure_report_retry_count'] = Variable<int>(failureReportRetryCount);
    if (!nullToAbsent || failureReportNextAttemptAt != null) {
      map['failure_report_next_attempt_at'] = Variable<DateTime>(
        failureReportNextAttemptAt,
      );
    }
    return map;
  }

  UploadItemsCompanion toCompanion(bool nullToAbsent) {
    return UploadItemsCompanion(
      localAssetId: Value(localAssetId),
      taskId: Value(taskId),
      bindingId: bindingId == null && nullToAbsent
          ? const Value.absent()
          : Value(bindingId),
      candidateId: candidateId == null && nullToAbsent
          ? const Value.absent()
          : Value(candidateId),
      localRefJson: Value(localRefJson),
      byteSize: Value(byteSize),
      sha256: sha256 == null && nullToAbsent
          ? const Value.absent()
          : Value(sha256),
      backendAssetId: backendAssetId == null && nullToAbsent
          ? const Value.absent()
          : Value(backendAssetId),
      providerUploadId: providerUploadId == null && nullToAbsent
          ? const Value.absent()
          : Value(providerUploadId),
      partSize: Value(partSize),
      retryCount: Value(retryCount),
      nextAttemptAt: nextAttemptAt == null && nullToAbsent
          ? const Value.absent()
          : Value(nextAttemptAt),
      state: Value(state),
      lastErrorCode: lastErrorCode == null && nullToAbsent
          ? const Value.absent()
          : Value(lastErrorCode),
      lastErrorMessage: lastErrorMessage == null && nullToAbsent
          ? const Value.absent()
          : Value(lastErrorMessage),
      leaseOwner: leaseOwner == null && nullToAbsent
          ? const Value.absent()
          : Value(leaseOwner),
      leaseExpiresAt: leaseExpiresAt == null && nullToAbsent
          ? const Value.absent()
          : Value(leaseExpiresAt),
      leaseGeneration: Value(leaseGeneration),
      policyGeneration: Value(policyGeneration),
      serverConfirmed: Value(serverConfirmed),
      terminalSummaryAcknowledged: Value(terminalSummaryAcknowledged),
      retryRequested: Value(retryRequested),
      localRejected: Value(localRejected),
      pausedFromState: pausedFromState == null && nullToAbsent
          ? const Value.absent()
          : Value(pausedFromState),
      failureReportRetryCount: Value(failureReportRetryCount),
      failureReportNextAttemptAt:
          failureReportNextAttemptAt == null && nullToAbsent
          ? const Value.absent()
          : Value(failureReportNextAttemptAt),
    );
  }

  factory UploadItem.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return UploadItem(
      localAssetId: serializer.fromJson<String>(json['localAssetId']),
      taskId: serializer.fromJson<String>(json['taskId']),
      bindingId: serializer.fromJson<String?>(json['bindingId']),
      candidateId: serializer.fromJson<String?>(json['candidateId']),
      localRefJson: serializer.fromJson<String>(json['localRefJson']),
      byteSize: serializer.fromJson<int>(json['byteSize']),
      sha256: serializer.fromJson<String?>(json['sha256']),
      backendAssetId: serializer.fromJson<String?>(json['backendAssetId']),
      providerUploadId: serializer.fromJson<String?>(json['providerUploadId']),
      partSize: serializer.fromJson<int>(json['partSize']),
      retryCount: serializer.fromJson<int>(json['retryCount']),
      nextAttemptAt: serializer.fromJson<DateTime?>(json['nextAttemptAt']),
      state: serializer.fromJson<String>(json['state']),
      lastErrorCode: serializer.fromJson<String?>(json['lastErrorCode']),
      lastErrorMessage: serializer.fromJson<String?>(json['lastErrorMessage']),
      leaseOwner: serializer.fromJson<String?>(json['leaseOwner']),
      leaseExpiresAt: serializer.fromJson<DateTime?>(json['leaseExpiresAt']),
      leaseGeneration: serializer.fromJson<int>(json['leaseGeneration']),
      policyGeneration: serializer.fromJson<int>(json['policyGeneration']),
      serverConfirmed: serializer.fromJson<bool>(json['serverConfirmed']),
      terminalSummaryAcknowledged: serializer.fromJson<bool>(
        json['terminalSummaryAcknowledged'],
      ),
      retryRequested: serializer.fromJson<bool>(json['retryRequested']),
      localRejected: serializer.fromJson<bool>(json['localRejected']),
      pausedFromState: serializer.fromJson<String?>(json['pausedFromState']),
      failureReportRetryCount: serializer.fromJson<int>(
        json['failureReportRetryCount'],
      ),
      failureReportNextAttemptAt: serializer.fromJson<DateTime?>(
        json['failureReportNextAttemptAt'],
      ),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'localAssetId': serializer.toJson<String>(localAssetId),
      'taskId': serializer.toJson<String>(taskId),
      'bindingId': serializer.toJson<String?>(bindingId),
      'candidateId': serializer.toJson<String?>(candidateId),
      'localRefJson': serializer.toJson<String>(localRefJson),
      'byteSize': serializer.toJson<int>(byteSize),
      'sha256': serializer.toJson<String?>(sha256),
      'backendAssetId': serializer.toJson<String?>(backendAssetId),
      'providerUploadId': serializer.toJson<String?>(providerUploadId),
      'partSize': serializer.toJson<int>(partSize),
      'retryCount': serializer.toJson<int>(retryCount),
      'nextAttemptAt': serializer.toJson<DateTime?>(nextAttemptAt),
      'state': serializer.toJson<String>(state),
      'lastErrorCode': serializer.toJson<String?>(lastErrorCode),
      'lastErrorMessage': serializer.toJson<String?>(lastErrorMessage),
      'leaseOwner': serializer.toJson<String?>(leaseOwner),
      'leaseExpiresAt': serializer.toJson<DateTime?>(leaseExpiresAt),
      'leaseGeneration': serializer.toJson<int>(leaseGeneration),
      'policyGeneration': serializer.toJson<int>(policyGeneration),
      'serverConfirmed': serializer.toJson<bool>(serverConfirmed),
      'terminalSummaryAcknowledged': serializer.toJson<bool>(
        terminalSummaryAcknowledged,
      ),
      'retryRequested': serializer.toJson<bool>(retryRequested),
      'localRejected': serializer.toJson<bool>(localRejected),
      'pausedFromState': serializer.toJson<String?>(pausedFromState),
      'failureReportRetryCount': serializer.toJson<int>(
        failureReportRetryCount,
      ),
      'failureReportNextAttemptAt': serializer.toJson<DateTime?>(
        failureReportNextAttemptAt,
      ),
    };
  }

  UploadItem copyWith({
    String? localAssetId,
    String? taskId,
    Value<String?> bindingId = const Value.absent(),
    Value<String?> candidateId = const Value.absent(),
    String? localRefJson,
    int? byteSize,
    Value<String?> sha256 = const Value.absent(),
    Value<String?> backendAssetId = const Value.absent(),
    Value<String?> providerUploadId = const Value.absent(),
    int? partSize,
    int? retryCount,
    Value<DateTime?> nextAttemptAt = const Value.absent(),
    String? state,
    Value<String?> lastErrorCode = const Value.absent(),
    Value<String?> lastErrorMessage = const Value.absent(),
    Value<String?> leaseOwner = const Value.absent(),
    Value<DateTime?> leaseExpiresAt = const Value.absent(),
    int? leaseGeneration,
    int? policyGeneration,
    bool? serverConfirmed,
    bool? terminalSummaryAcknowledged,
    bool? retryRequested,
    bool? localRejected,
    Value<String?> pausedFromState = const Value.absent(),
    int? failureReportRetryCount,
    Value<DateTime?> failureReportNextAttemptAt = const Value.absent(),
  }) => UploadItem(
    localAssetId: localAssetId ?? this.localAssetId,
    taskId: taskId ?? this.taskId,
    bindingId: bindingId.present ? bindingId.value : this.bindingId,
    candidateId: candidateId.present ? candidateId.value : this.candidateId,
    localRefJson: localRefJson ?? this.localRefJson,
    byteSize: byteSize ?? this.byteSize,
    sha256: sha256.present ? sha256.value : this.sha256,
    backendAssetId: backendAssetId.present
        ? backendAssetId.value
        : this.backendAssetId,
    providerUploadId: providerUploadId.present
        ? providerUploadId.value
        : this.providerUploadId,
    partSize: partSize ?? this.partSize,
    retryCount: retryCount ?? this.retryCount,
    nextAttemptAt: nextAttemptAt.present
        ? nextAttemptAt.value
        : this.nextAttemptAt,
    state: state ?? this.state,
    lastErrorCode: lastErrorCode.present
        ? lastErrorCode.value
        : this.lastErrorCode,
    lastErrorMessage: lastErrorMessage.present
        ? lastErrorMessage.value
        : this.lastErrorMessage,
    leaseOwner: leaseOwner.present ? leaseOwner.value : this.leaseOwner,
    leaseExpiresAt: leaseExpiresAt.present
        ? leaseExpiresAt.value
        : this.leaseExpiresAt,
    leaseGeneration: leaseGeneration ?? this.leaseGeneration,
    policyGeneration: policyGeneration ?? this.policyGeneration,
    serverConfirmed: serverConfirmed ?? this.serverConfirmed,
    terminalSummaryAcknowledged:
        terminalSummaryAcknowledged ?? this.terminalSummaryAcknowledged,
    retryRequested: retryRequested ?? this.retryRequested,
    localRejected: localRejected ?? this.localRejected,
    pausedFromState: pausedFromState.present
        ? pausedFromState.value
        : this.pausedFromState,
    failureReportRetryCount:
        failureReportRetryCount ?? this.failureReportRetryCount,
    failureReportNextAttemptAt: failureReportNextAttemptAt.present
        ? failureReportNextAttemptAt.value
        : this.failureReportNextAttemptAt,
  );
  UploadItem copyWithCompanion(UploadItemsCompanion data) {
    return UploadItem(
      localAssetId: data.localAssetId.present
          ? data.localAssetId.value
          : this.localAssetId,
      taskId: data.taskId.present ? data.taskId.value : this.taskId,
      bindingId: data.bindingId.present ? data.bindingId.value : this.bindingId,
      candidateId: data.candidateId.present
          ? data.candidateId.value
          : this.candidateId,
      localRefJson: data.localRefJson.present
          ? data.localRefJson.value
          : this.localRefJson,
      byteSize: data.byteSize.present ? data.byteSize.value : this.byteSize,
      sha256: data.sha256.present ? data.sha256.value : this.sha256,
      backendAssetId: data.backendAssetId.present
          ? data.backendAssetId.value
          : this.backendAssetId,
      providerUploadId: data.providerUploadId.present
          ? data.providerUploadId.value
          : this.providerUploadId,
      partSize: data.partSize.present ? data.partSize.value : this.partSize,
      retryCount: data.retryCount.present
          ? data.retryCount.value
          : this.retryCount,
      nextAttemptAt: data.nextAttemptAt.present
          ? data.nextAttemptAt.value
          : this.nextAttemptAt,
      state: data.state.present ? data.state.value : this.state,
      lastErrorCode: data.lastErrorCode.present
          ? data.lastErrorCode.value
          : this.lastErrorCode,
      lastErrorMessage: data.lastErrorMessage.present
          ? data.lastErrorMessage.value
          : this.lastErrorMessage,
      leaseOwner: data.leaseOwner.present
          ? data.leaseOwner.value
          : this.leaseOwner,
      leaseExpiresAt: data.leaseExpiresAt.present
          ? data.leaseExpiresAt.value
          : this.leaseExpiresAt,
      leaseGeneration: data.leaseGeneration.present
          ? data.leaseGeneration.value
          : this.leaseGeneration,
      policyGeneration: data.policyGeneration.present
          ? data.policyGeneration.value
          : this.policyGeneration,
      serverConfirmed: data.serverConfirmed.present
          ? data.serverConfirmed.value
          : this.serverConfirmed,
      terminalSummaryAcknowledged: data.terminalSummaryAcknowledged.present
          ? data.terminalSummaryAcknowledged.value
          : this.terminalSummaryAcknowledged,
      retryRequested: data.retryRequested.present
          ? data.retryRequested.value
          : this.retryRequested,
      localRejected: data.localRejected.present
          ? data.localRejected.value
          : this.localRejected,
      pausedFromState: data.pausedFromState.present
          ? data.pausedFromState.value
          : this.pausedFromState,
      failureReportRetryCount: data.failureReportRetryCount.present
          ? data.failureReportRetryCount.value
          : this.failureReportRetryCount,
      failureReportNextAttemptAt: data.failureReportNextAttemptAt.present
          ? data.failureReportNextAttemptAt.value
          : this.failureReportNextAttemptAt,
    );
  }

  @override
  String toString() {
    return (StringBuffer('UploadItem(')
          ..write('localAssetId: $localAssetId, ')
          ..write('taskId: $taskId, ')
          ..write('bindingId: $bindingId, ')
          ..write('candidateId: $candidateId, ')
          ..write('localRefJson: $localRefJson, ')
          ..write('byteSize: $byteSize, ')
          ..write('sha256: $sha256, ')
          ..write('backendAssetId: $backendAssetId, ')
          ..write('providerUploadId: $providerUploadId, ')
          ..write('partSize: $partSize, ')
          ..write('retryCount: $retryCount, ')
          ..write('nextAttemptAt: $nextAttemptAt, ')
          ..write('state: $state, ')
          ..write('lastErrorCode: $lastErrorCode, ')
          ..write('lastErrorMessage: $lastErrorMessage, ')
          ..write('leaseOwner: $leaseOwner, ')
          ..write('leaseExpiresAt: $leaseExpiresAt, ')
          ..write('leaseGeneration: $leaseGeneration, ')
          ..write('policyGeneration: $policyGeneration, ')
          ..write('serverConfirmed: $serverConfirmed, ')
          ..write('terminalSummaryAcknowledged: $terminalSummaryAcknowledged, ')
          ..write('retryRequested: $retryRequested, ')
          ..write('localRejected: $localRejected, ')
          ..write('pausedFromState: $pausedFromState, ')
          ..write('failureReportRetryCount: $failureReportRetryCount, ')
          ..write('failureReportNextAttemptAt: $failureReportNextAttemptAt')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hashAll([
    localAssetId,
    taskId,
    bindingId,
    candidateId,
    localRefJson,
    byteSize,
    sha256,
    backendAssetId,
    providerUploadId,
    partSize,
    retryCount,
    nextAttemptAt,
    state,
    lastErrorCode,
    lastErrorMessage,
    leaseOwner,
    leaseExpiresAt,
    leaseGeneration,
    policyGeneration,
    serverConfirmed,
    terminalSummaryAcknowledged,
    retryRequested,
    localRejected,
    pausedFromState,
    failureReportRetryCount,
    failureReportNextAttemptAt,
  ]);
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is UploadItem &&
          other.localAssetId == this.localAssetId &&
          other.taskId == this.taskId &&
          other.bindingId == this.bindingId &&
          other.candidateId == this.candidateId &&
          other.localRefJson == this.localRefJson &&
          other.byteSize == this.byteSize &&
          other.sha256 == this.sha256 &&
          other.backendAssetId == this.backendAssetId &&
          other.providerUploadId == this.providerUploadId &&
          other.partSize == this.partSize &&
          other.retryCount == this.retryCount &&
          other.nextAttemptAt == this.nextAttemptAt &&
          other.state == this.state &&
          other.lastErrorCode == this.lastErrorCode &&
          other.lastErrorMessage == this.lastErrorMessage &&
          other.leaseOwner == this.leaseOwner &&
          other.leaseExpiresAt == this.leaseExpiresAt &&
          other.leaseGeneration == this.leaseGeneration &&
          other.policyGeneration == this.policyGeneration &&
          other.serverConfirmed == this.serverConfirmed &&
          other.terminalSummaryAcknowledged ==
              this.terminalSummaryAcknowledged &&
          other.retryRequested == this.retryRequested &&
          other.localRejected == this.localRejected &&
          other.pausedFromState == this.pausedFromState &&
          other.failureReportRetryCount == this.failureReportRetryCount &&
          other.failureReportNextAttemptAt == this.failureReportNextAttemptAt);
}

class UploadItemsCompanion extends UpdateCompanion<UploadItem> {
  final Value<String> localAssetId;
  final Value<String> taskId;
  final Value<String?> bindingId;
  final Value<String?> candidateId;
  final Value<String> localRefJson;
  final Value<int> byteSize;
  final Value<String?> sha256;
  final Value<String?> backendAssetId;
  final Value<String?> providerUploadId;
  final Value<int> partSize;
  final Value<int> retryCount;
  final Value<DateTime?> nextAttemptAt;
  final Value<String> state;
  final Value<String?> lastErrorCode;
  final Value<String?> lastErrorMessage;
  final Value<String?> leaseOwner;
  final Value<DateTime?> leaseExpiresAt;
  final Value<int> leaseGeneration;
  final Value<int> policyGeneration;
  final Value<bool> serverConfirmed;
  final Value<bool> terminalSummaryAcknowledged;
  final Value<bool> retryRequested;
  final Value<bool> localRejected;
  final Value<String?> pausedFromState;
  final Value<int> failureReportRetryCount;
  final Value<DateTime?> failureReportNextAttemptAt;
  final Value<int> rowid;
  const UploadItemsCompanion({
    this.localAssetId = const Value.absent(),
    this.taskId = const Value.absent(),
    this.bindingId = const Value.absent(),
    this.candidateId = const Value.absent(),
    this.localRefJson = const Value.absent(),
    this.byteSize = const Value.absent(),
    this.sha256 = const Value.absent(),
    this.backendAssetId = const Value.absent(),
    this.providerUploadId = const Value.absent(),
    this.partSize = const Value.absent(),
    this.retryCount = const Value.absent(),
    this.nextAttemptAt = const Value.absent(),
    this.state = const Value.absent(),
    this.lastErrorCode = const Value.absent(),
    this.lastErrorMessage = const Value.absent(),
    this.leaseOwner = const Value.absent(),
    this.leaseExpiresAt = const Value.absent(),
    this.leaseGeneration = const Value.absent(),
    this.policyGeneration = const Value.absent(),
    this.serverConfirmed = const Value.absent(),
    this.terminalSummaryAcknowledged = const Value.absent(),
    this.retryRequested = const Value.absent(),
    this.localRejected = const Value.absent(),
    this.pausedFromState = const Value.absent(),
    this.failureReportRetryCount = const Value.absent(),
    this.failureReportNextAttemptAt = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  UploadItemsCompanion.insert({
    required String localAssetId,
    required String taskId,
    this.bindingId = const Value.absent(),
    this.candidateId = const Value.absent(),
    required String localRefJson,
    required int byteSize,
    this.sha256 = const Value.absent(),
    this.backendAssetId = const Value.absent(),
    this.providerUploadId = const Value.absent(),
    required int partSize,
    this.retryCount = const Value.absent(),
    this.nextAttemptAt = const Value.absent(),
    required String state,
    this.lastErrorCode = const Value.absent(),
    this.lastErrorMessage = const Value.absent(),
    this.leaseOwner = const Value.absent(),
    this.leaseExpiresAt = const Value.absent(),
    this.leaseGeneration = const Value.absent(),
    this.policyGeneration = const Value.absent(),
    this.serverConfirmed = const Value.absent(),
    this.terminalSummaryAcknowledged = const Value.absent(),
    this.retryRequested = const Value.absent(),
    this.localRejected = const Value.absent(),
    this.pausedFromState = const Value.absent(),
    this.failureReportRetryCount = const Value.absent(),
    this.failureReportNextAttemptAt = const Value.absent(),
    this.rowid = const Value.absent(),
  }) : localAssetId = Value(localAssetId),
       taskId = Value(taskId),
       localRefJson = Value(localRefJson),
       byteSize = Value(byteSize),
       partSize = Value(partSize),
       state = Value(state);
  static Insertable<UploadItem> custom({
    Expression<String>? localAssetId,
    Expression<String>? taskId,
    Expression<String>? bindingId,
    Expression<String>? candidateId,
    Expression<String>? localRefJson,
    Expression<int>? byteSize,
    Expression<String>? sha256,
    Expression<String>? backendAssetId,
    Expression<String>? providerUploadId,
    Expression<int>? partSize,
    Expression<int>? retryCount,
    Expression<DateTime>? nextAttemptAt,
    Expression<String>? state,
    Expression<String>? lastErrorCode,
    Expression<String>? lastErrorMessage,
    Expression<String>? leaseOwner,
    Expression<DateTime>? leaseExpiresAt,
    Expression<int>? leaseGeneration,
    Expression<int>? policyGeneration,
    Expression<bool>? serverConfirmed,
    Expression<bool>? terminalSummaryAcknowledged,
    Expression<bool>? retryRequested,
    Expression<bool>? localRejected,
    Expression<String>? pausedFromState,
    Expression<int>? failureReportRetryCount,
    Expression<DateTime>? failureReportNextAttemptAt,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (localAssetId != null) 'local_asset_id': localAssetId,
      if (taskId != null) 'task_id': taskId,
      if (bindingId != null) 'binding_id': bindingId,
      if (candidateId != null) 'candidate_id': candidateId,
      if (localRefJson != null) 'local_ref_json': localRefJson,
      if (byteSize != null) 'byte_size': byteSize,
      if (sha256 != null) 'sha256': sha256,
      if (backendAssetId != null) 'backend_asset_id': backendAssetId,
      if (providerUploadId != null) 'provider_upload_id': providerUploadId,
      if (partSize != null) 'part_size': partSize,
      if (retryCount != null) 'retry_count': retryCount,
      if (nextAttemptAt != null) 'next_attempt_at': nextAttemptAt,
      if (state != null) 'state': state,
      if (lastErrorCode != null) 'last_error_code': lastErrorCode,
      if (lastErrorMessage != null) 'last_error_message': lastErrorMessage,
      if (leaseOwner != null) 'lease_owner': leaseOwner,
      if (leaseExpiresAt != null) 'lease_expires_at': leaseExpiresAt,
      if (leaseGeneration != null) 'lease_generation': leaseGeneration,
      if (policyGeneration != null) 'policy_generation': policyGeneration,
      if (serverConfirmed != null) 'server_confirmed': serverConfirmed,
      if (terminalSummaryAcknowledged != null)
        'terminal_summary_acknowledged': terminalSummaryAcknowledged,
      if (retryRequested != null) 'retry_requested': retryRequested,
      if (localRejected != null) 'local_rejected': localRejected,
      if (pausedFromState != null) 'paused_from_state': pausedFromState,
      if (failureReportRetryCount != null)
        'failure_report_retry_count': failureReportRetryCount,
      if (failureReportNextAttemptAt != null)
        'failure_report_next_attempt_at': failureReportNextAttemptAt,
      if (rowid != null) 'rowid': rowid,
    });
  }

  UploadItemsCompanion copyWith({
    Value<String>? localAssetId,
    Value<String>? taskId,
    Value<String?>? bindingId,
    Value<String?>? candidateId,
    Value<String>? localRefJson,
    Value<int>? byteSize,
    Value<String?>? sha256,
    Value<String?>? backendAssetId,
    Value<String?>? providerUploadId,
    Value<int>? partSize,
    Value<int>? retryCount,
    Value<DateTime?>? nextAttemptAt,
    Value<String>? state,
    Value<String?>? lastErrorCode,
    Value<String?>? lastErrorMessage,
    Value<String?>? leaseOwner,
    Value<DateTime?>? leaseExpiresAt,
    Value<int>? leaseGeneration,
    Value<int>? policyGeneration,
    Value<bool>? serverConfirmed,
    Value<bool>? terminalSummaryAcknowledged,
    Value<bool>? retryRequested,
    Value<bool>? localRejected,
    Value<String?>? pausedFromState,
    Value<int>? failureReportRetryCount,
    Value<DateTime?>? failureReportNextAttemptAt,
    Value<int>? rowid,
  }) {
    return UploadItemsCompanion(
      localAssetId: localAssetId ?? this.localAssetId,
      taskId: taskId ?? this.taskId,
      bindingId: bindingId ?? this.bindingId,
      candidateId: candidateId ?? this.candidateId,
      localRefJson: localRefJson ?? this.localRefJson,
      byteSize: byteSize ?? this.byteSize,
      sha256: sha256 ?? this.sha256,
      backendAssetId: backendAssetId ?? this.backendAssetId,
      providerUploadId: providerUploadId ?? this.providerUploadId,
      partSize: partSize ?? this.partSize,
      retryCount: retryCount ?? this.retryCount,
      nextAttemptAt: nextAttemptAt ?? this.nextAttemptAt,
      state: state ?? this.state,
      lastErrorCode: lastErrorCode ?? this.lastErrorCode,
      lastErrorMessage: lastErrorMessage ?? this.lastErrorMessage,
      leaseOwner: leaseOwner ?? this.leaseOwner,
      leaseExpiresAt: leaseExpiresAt ?? this.leaseExpiresAt,
      leaseGeneration: leaseGeneration ?? this.leaseGeneration,
      policyGeneration: policyGeneration ?? this.policyGeneration,
      serverConfirmed: serverConfirmed ?? this.serverConfirmed,
      terminalSummaryAcknowledged:
          terminalSummaryAcknowledged ?? this.terminalSummaryAcknowledged,
      retryRequested: retryRequested ?? this.retryRequested,
      localRejected: localRejected ?? this.localRejected,
      pausedFromState: pausedFromState ?? this.pausedFromState,
      failureReportRetryCount:
          failureReportRetryCount ?? this.failureReportRetryCount,
      failureReportNextAttemptAt:
          failureReportNextAttemptAt ?? this.failureReportNextAttemptAt,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (localAssetId.present) {
      map['local_asset_id'] = Variable<String>(localAssetId.value);
    }
    if (taskId.present) {
      map['task_id'] = Variable<String>(taskId.value);
    }
    if (bindingId.present) {
      map['binding_id'] = Variable<String>(bindingId.value);
    }
    if (candidateId.present) {
      map['candidate_id'] = Variable<String>(candidateId.value);
    }
    if (localRefJson.present) {
      map['local_ref_json'] = Variable<String>(localRefJson.value);
    }
    if (byteSize.present) {
      map['byte_size'] = Variable<int>(byteSize.value);
    }
    if (sha256.present) {
      map['sha256'] = Variable<String>(sha256.value);
    }
    if (backendAssetId.present) {
      map['backend_asset_id'] = Variable<String>(backendAssetId.value);
    }
    if (providerUploadId.present) {
      map['provider_upload_id'] = Variable<String>(providerUploadId.value);
    }
    if (partSize.present) {
      map['part_size'] = Variable<int>(partSize.value);
    }
    if (retryCount.present) {
      map['retry_count'] = Variable<int>(retryCount.value);
    }
    if (nextAttemptAt.present) {
      map['next_attempt_at'] = Variable<DateTime>(nextAttemptAt.value);
    }
    if (state.present) {
      map['state'] = Variable<String>(state.value);
    }
    if (lastErrorCode.present) {
      map['last_error_code'] = Variable<String>(lastErrorCode.value);
    }
    if (lastErrorMessage.present) {
      map['last_error_message'] = Variable<String>(lastErrorMessage.value);
    }
    if (leaseOwner.present) {
      map['lease_owner'] = Variable<String>(leaseOwner.value);
    }
    if (leaseExpiresAt.present) {
      map['lease_expires_at'] = Variable<DateTime>(leaseExpiresAt.value);
    }
    if (leaseGeneration.present) {
      map['lease_generation'] = Variable<int>(leaseGeneration.value);
    }
    if (policyGeneration.present) {
      map['policy_generation'] = Variable<int>(policyGeneration.value);
    }
    if (serverConfirmed.present) {
      map['server_confirmed'] = Variable<bool>(serverConfirmed.value);
    }
    if (terminalSummaryAcknowledged.present) {
      map['terminal_summary_acknowledged'] = Variable<bool>(
        terminalSummaryAcknowledged.value,
      );
    }
    if (retryRequested.present) {
      map['retry_requested'] = Variable<bool>(retryRequested.value);
    }
    if (localRejected.present) {
      map['local_rejected'] = Variable<bool>(localRejected.value);
    }
    if (pausedFromState.present) {
      map['paused_from_state'] = Variable<String>(pausedFromState.value);
    }
    if (failureReportRetryCount.present) {
      map['failure_report_retry_count'] = Variable<int>(
        failureReportRetryCount.value,
      );
    }
    if (failureReportNextAttemptAt.present) {
      map['failure_report_next_attempt_at'] = Variable<DateTime>(
        failureReportNextAttemptAt.value,
      );
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('UploadItemsCompanion(')
          ..write('localAssetId: $localAssetId, ')
          ..write('taskId: $taskId, ')
          ..write('bindingId: $bindingId, ')
          ..write('candidateId: $candidateId, ')
          ..write('localRefJson: $localRefJson, ')
          ..write('byteSize: $byteSize, ')
          ..write('sha256: $sha256, ')
          ..write('backendAssetId: $backendAssetId, ')
          ..write('providerUploadId: $providerUploadId, ')
          ..write('partSize: $partSize, ')
          ..write('retryCount: $retryCount, ')
          ..write('nextAttemptAt: $nextAttemptAt, ')
          ..write('state: $state, ')
          ..write('lastErrorCode: $lastErrorCode, ')
          ..write('lastErrorMessage: $lastErrorMessage, ')
          ..write('leaseOwner: $leaseOwner, ')
          ..write('leaseExpiresAt: $leaseExpiresAt, ')
          ..write('leaseGeneration: $leaseGeneration, ')
          ..write('policyGeneration: $policyGeneration, ')
          ..write('serverConfirmed: $serverConfirmed, ')
          ..write('terminalSummaryAcknowledged: $terminalSummaryAcknowledged, ')
          ..write('retryRequested: $retryRequested, ')
          ..write('localRejected: $localRejected, ')
          ..write('pausedFromState: $pausedFromState, ')
          ..write('failureReportRetryCount: $failureReportRetryCount, ')
          ..write('failureReportNextAttemptAt: $failureReportNextAttemptAt, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $UploadPartsTable extends UploadParts
    with TableInfo<$UploadPartsTable, UploadPart> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $UploadPartsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _localAssetIdMeta = const VerificationMeta(
    'localAssetId',
  );
  @override
  late final GeneratedColumn<String> localAssetId = GeneratedColumn<String>(
    'local_asset_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'REFERENCES upload_items (local_asset_id) ON DELETE CASCADE',
    ),
  );
  static const VerificationMeta _partNumberMeta = const VerificationMeta(
    'partNumber',
  );
  @override
  late final GeneratedColumn<int> partNumber = GeneratedColumn<int>(
    'part_number',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _etagMeta = const VerificationMeta('etag');
  @override
  late final GeneratedColumn<String> etag = GeneratedColumn<String>(
    'etag',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _nativeJobIdMeta = const VerificationMeta(
    'nativeJobId',
  );
  @override
  late final GeneratedColumn<String> nativeJobId = GeneratedColumn<String>(
    'native_job_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _nativeLeaseOwnerMeta = const VerificationMeta(
    'nativeLeaseOwner',
  );
  @override
  late final GeneratedColumn<String> nativeLeaseOwner = GeneratedColumn<String>(
    'native_lease_owner',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _nativeLeaseGenerationMeta =
      const VerificationMeta('nativeLeaseGeneration');
  @override
  late final GeneratedColumn<int> nativeLeaseGeneration = GeneratedColumn<int>(
    'native_lease_generation',
    aliasedName,
    true,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _nativePolicyGenerationMeta =
      const VerificationMeta('nativePolicyGeneration');
  @override
  late final GeneratedColumn<int> nativePolicyGeneration = GeneratedColumn<int>(
    'native_policy_generation',
    aliasedName,
    true,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
  );
  @override
  List<GeneratedColumn> get $columns => [
    localAssetId,
    partNumber,
    etag,
    nativeJobId,
    nativeLeaseOwner,
    nativeLeaseGeneration,
    nativePolicyGeneration,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'upload_parts';
  @override
  VerificationContext validateIntegrity(
    Insertable<UploadPart> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('local_asset_id')) {
      context.handle(
        _localAssetIdMeta,
        localAssetId.isAcceptableOrUnknown(
          data['local_asset_id']!,
          _localAssetIdMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_localAssetIdMeta);
    }
    if (data.containsKey('part_number')) {
      context.handle(
        _partNumberMeta,
        partNumber.isAcceptableOrUnknown(data['part_number']!, _partNumberMeta),
      );
    } else if (isInserting) {
      context.missing(_partNumberMeta);
    }
    if (data.containsKey('etag')) {
      context.handle(
        _etagMeta,
        etag.isAcceptableOrUnknown(data['etag']!, _etagMeta),
      );
    }
    if (data.containsKey('native_job_id')) {
      context.handle(
        _nativeJobIdMeta,
        nativeJobId.isAcceptableOrUnknown(
          data['native_job_id']!,
          _nativeJobIdMeta,
        ),
      );
    }
    if (data.containsKey('native_lease_owner')) {
      context.handle(
        _nativeLeaseOwnerMeta,
        nativeLeaseOwner.isAcceptableOrUnknown(
          data['native_lease_owner']!,
          _nativeLeaseOwnerMeta,
        ),
      );
    }
    if (data.containsKey('native_lease_generation')) {
      context.handle(
        _nativeLeaseGenerationMeta,
        nativeLeaseGeneration.isAcceptableOrUnknown(
          data['native_lease_generation']!,
          _nativeLeaseGenerationMeta,
        ),
      );
    }
    if (data.containsKey('native_policy_generation')) {
      context.handle(
        _nativePolicyGenerationMeta,
        nativePolicyGeneration.isAcceptableOrUnknown(
          data['native_policy_generation']!,
          _nativePolicyGenerationMeta,
        ),
      );
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {localAssetId, partNumber};
  @override
  UploadPart map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return UploadPart(
      localAssetId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}local_asset_id'],
      )!,
      partNumber: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}part_number'],
      )!,
      etag: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}etag'],
      ),
      nativeJobId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}native_job_id'],
      ),
      nativeLeaseOwner: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}native_lease_owner'],
      ),
      nativeLeaseGeneration: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}native_lease_generation'],
      ),
      nativePolicyGeneration: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}native_policy_generation'],
      ),
    );
  }

  @override
  $UploadPartsTable createAlias(String alias) {
    return $UploadPartsTable(attachedDatabase, alias);
  }
}

class UploadPart extends DataClass implements Insertable<UploadPart> {
  final String localAssetId;
  final int partNumber;
  final String? etag;
  final String? nativeJobId;
  final String? nativeLeaseOwner;
  final int? nativeLeaseGeneration;
  final int? nativePolicyGeneration;
  const UploadPart({
    required this.localAssetId,
    required this.partNumber,
    this.etag,
    this.nativeJobId,
    this.nativeLeaseOwner,
    this.nativeLeaseGeneration,
    this.nativePolicyGeneration,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['local_asset_id'] = Variable<String>(localAssetId);
    map['part_number'] = Variable<int>(partNumber);
    if (!nullToAbsent || etag != null) {
      map['etag'] = Variable<String>(etag);
    }
    if (!nullToAbsent || nativeJobId != null) {
      map['native_job_id'] = Variable<String>(nativeJobId);
    }
    if (!nullToAbsent || nativeLeaseOwner != null) {
      map['native_lease_owner'] = Variable<String>(nativeLeaseOwner);
    }
    if (!nullToAbsent || nativeLeaseGeneration != null) {
      map['native_lease_generation'] = Variable<int>(nativeLeaseGeneration);
    }
    if (!nullToAbsent || nativePolicyGeneration != null) {
      map['native_policy_generation'] = Variable<int>(nativePolicyGeneration);
    }
    return map;
  }

  UploadPartsCompanion toCompanion(bool nullToAbsent) {
    return UploadPartsCompanion(
      localAssetId: Value(localAssetId),
      partNumber: Value(partNumber),
      etag: etag == null && nullToAbsent ? const Value.absent() : Value(etag),
      nativeJobId: nativeJobId == null && nullToAbsent
          ? const Value.absent()
          : Value(nativeJobId),
      nativeLeaseOwner: nativeLeaseOwner == null && nullToAbsent
          ? const Value.absent()
          : Value(nativeLeaseOwner),
      nativeLeaseGeneration: nativeLeaseGeneration == null && nullToAbsent
          ? const Value.absent()
          : Value(nativeLeaseGeneration),
      nativePolicyGeneration: nativePolicyGeneration == null && nullToAbsent
          ? const Value.absent()
          : Value(nativePolicyGeneration),
    );
  }

  factory UploadPart.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return UploadPart(
      localAssetId: serializer.fromJson<String>(json['localAssetId']),
      partNumber: serializer.fromJson<int>(json['partNumber']),
      etag: serializer.fromJson<String?>(json['etag']),
      nativeJobId: serializer.fromJson<String?>(json['nativeJobId']),
      nativeLeaseOwner: serializer.fromJson<String?>(json['nativeLeaseOwner']),
      nativeLeaseGeneration: serializer.fromJson<int?>(
        json['nativeLeaseGeneration'],
      ),
      nativePolicyGeneration: serializer.fromJson<int?>(
        json['nativePolicyGeneration'],
      ),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'localAssetId': serializer.toJson<String>(localAssetId),
      'partNumber': serializer.toJson<int>(partNumber),
      'etag': serializer.toJson<String?>(etag),
      'nativeJobId': serializer.toJson<String?>(nativeJobId),
      'nativeLeaseOwner': serializer.toJson<String?>(nativeLeaseOwner),
      'nativeLeaseGeneration': serializer.toJson<int?>(nativeLeaseGeneration),
      'nativePolicyGeneration': serializer.toJson<int?>(nativePolicyGeneration),
    };
  }

  UploadPart copyWith({
    String? localAssetId,
    int? partNumber,
    Value<String?> etag = const Value.absent(),
    Value<String?> nativeJobId = const Value.absent(),
    Value<String?> nativeLeaseOwner = const Value.absent(),
    Value<int?> nativeLeaseGeneration = const Value.absent(),
    Value<int?> nativePolicyGeneration = const Value.absent(),
  }) => UploadPart(
    localAssetId: localAssetId ?? this.localAssetId,
    partNumber: partNumber ?? this.partNumber,
    etag: etag.present ? etag.value : this.etag,
    nativeJobId: nativeJobId.present ? nativeJobId.value : this.nativeJobId,
    nativeLeaseOwner: nativeLeaseOwner.present
        ? nativeLeaseOwner.value
        : this.nativeLeaseOwner,
    nativeLeaseGeneration: nativeLeaseGeneration.present
        ? nativeLeaseGeneration.value
        : this.nativeLeaseGeneration,
    nativePolicyGeneration: nativePolicyGeneration.present
        ? nativePolicyGeneration.value
        : this.nativePolicyGeneration,
  );
  UploadPart copyWithCompanion(UploadPartsCompanion data) {
    return UploadPart(
      localAssetId: data.localAssetId.present
          ? data.localAssetId.value
          : this.localAssetId,
      partNumber: data.partNumber.present
          ? data.partNumber.value
          : this.partNumber,
      etag: data.etag.present ? data.etag.value : this.etag,
      nativeJobId: data.nativeJobId.present
          ? data.nativeJobId.value
          : this.nativeJobId,
      nativeLeaseOwner: data.nativeLeaseOwner.present
          ? data.nativeLeaseOwner.value
          : this.nativeLeaseOwner,
      nativeLeaseGeneration: data.nativeLeaseGeneration.present
          ? data.nativeLeaseGeneration.value
          : this.nativeLeaseGeneration,
      nativePolicyGeneration: data.nativePolicyGeneration.present
          ? data.nativePolicyGeneration.value
          : this.nativePolicyGeneration,
    );
  }

  @override
  String toString() {
    return (StringBuffer('UploadPart(')
          ..write('localAssetId: $localAssetId, ')
          ..write('partNumber: $partNumber, ')
          ..write('etag: $etag, ')
          ..write('nativeJobId: $nativeJobId, ')
          ..write('nativeLeaseOwner: $nativeLeaseOwner, ')
          ..write('nativeLeaseGeneration: $nativeLeaseGeneration, ')
          ..write('nativePolicyGeneration: $nativePolicyGeneration')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    localAssetId,
    partNumber,
    etag,
    nativeJobId,
    nativeLeaseOwner,
    nativeLeaseGeneration,
    nativePolicyGeneration,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is UploadPart &&
          other.localAssetId == this.localAssetId &&
          other.partNumber == this.partNumber &&
          other.etag == this.etag &&
          other.nativeJobId == this.nativeJobId &&
          other.nativeLeaseOwner == this.nativeLeaseOwner &&
          other.nativeLeaseGeneration == this.nativeLeaseGeneration &&
          other.nativePolicyGeneration == this.nativePolicyGeneration);
}

class UploadPartsCompanion extends UpdateCompanion<UploadPart> {
  final Value<String> localAssetId;
  final Value<int> partNumber;
  final Value<String?> etag;
  final Value<String?> nativeJobId;
  final Value<String?> nativeLeaseOwner;
  final Value<int?> nativeLeaseGeneration;
  final Value<int?> nativePolicyGeneration;
  final Value<int> rowid;
  const UploadPartsCompanion({
    this.localAssetId = const Value.absent(),
    this.partNumber = const Value.absent(),
    this.etag = const Value.absent(),
    this.nativeJobId = const Value.absent(),
    this.nativeLeaseOwner = const Value.absent(),
    this.nativeLeaseGeneration = const Value.absent(),
    this.nativePolicyGeneration = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  UploadPartsCompanion.insert({
    required String localAssetId,
    required int partNumber,
    this.etag = const Value.absent(),
    this.nativeJobId = const Value.absent(),
    this.nativeLeaseOwner = const Value.absent(),
    this.nativeLeaseGeneration = const Value.absent(),
    this.nativePolicyGeneration = const Value.absent(),
    this.rowid = const Value.absent(),
  }) : localAssetId = Value(localAssetId),
       partNumber = Value(partNumber);
  static Insertable<UploadPart> custom({
    Expression<String>? localAssetId,
    Expression<int>? partNumber,
    Expression<String>? etag,
    Expression<String>? nativeJobId,
    Expression<String>? nativeLeaseOwner,
    Expression<int>? nativeLeaseGeneration,
    Expression<int>? nativePolicyGeneration,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (localAssetId != null) 'local_asset_id': localAssetId,
      if (partNumber != null) 'part_number': partNumber,
      if (etag != null) 'etag': etag,
      if (nativeJobId != null) 'native_job_id': nativeJobId,
      if (nativeLeaseOwner != null) 'native_lease_owner': nativeLeaseOwner,
      if (nativeLeaseGeneration != null)
        'native_lease_generation': nativeLeaseGeneration,
      if (nativePolicyGeneration != null)
        'native_policy_generation': nativePolicyGeneration,
      if (rowid != null) 'rowid': rowid,
    });
  }

  UploadPartsCompanion copyWith({
    Value<String>? localAssetId,
    Value<int>? partNumber,
    Value<String?>? etag,
    Value<String?>? nativeJobId,
    Value<String?>? nativeLeaseOwner,
    Value<int?>? nativeLeaseGeneration,
    Value<int?>? nativePolicyGeneration,
    Value<int>? rowid,
  }) {
    return UploadPartsCompanion(
      localAssetId: localAssetId ?? this.localAssetId,
      partNumber: partNumber ?? this.partNumber,
      etag: etag ?? this.etag,
      nativeJobId: nativeJobId ?? this.nativeJobId,
      nativeLeaseOwner: nativeLeaseOwner ?? this.nativeLeaseOwner,
      nativeLeaseGeneration:
          nativeLeaseGeneration ?? this.nativeLeaseGeneration,
      nativePolicyGeneration:
          nativePolicyGeneration ?? this.nativePolicyGeneration,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (localAssetId.present) {
      map['local_asset_id'] = Variable<String>(localAssetId.value);
    }
    if (partNumber.present) {
      map['part_number'] = Variable<int>(partNumber.value);
    }
    if (etag.present) {
      map['etag'] = Variable<String>(etag.value);
    }
    if (nativeJobId.present) {
      map['native_job_id'] = Variable<String>(nativeJobId.value);
    }
    if (nativeLeaseOwner.present) {
      map['native_lease_owner'] = Variable<String>(nativeLeaseOwner.value);
    }
    if (nativeLeaseGeneration.present) {
      map['native_lease_generation'] = Variable<int>(
        nativeLeaseGeneration.value,
      );
    }
    if (nativePolicyGeneration.present) {
      map['native_policy_generation'] = Variable<int>(
        nativePolicyGeneration.value,
      );
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('UploadPartsCompanion(')
          ..write('localAssetId: $localAssetId, ')
          ..write('partNumber: $partNumber, ')
          ..write('etag: $etag, ')
          ..write('nativeJobId: $nativeJobId, ')
          ..write('nativeLeaseOwner: $nativeLeaseOwner, ')
          ..write('nativeLeaseGeneration: $nativeLeaseGeneration, ')
          ..write('nativePolicyGeneration: $nativePolicyGeneration, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $ImportQueueTasksTable extends ImportQueueTasks
    with TableInfo<$ImportQueueTasksTable, ImportQueueTask> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $ImportQueueTasksTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _taskIdMeta = const VerificationMeta('taskId');
  @override
  late final GeneratedColumn<String> taskId = GeneratedColumn<String>(
    'task_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _sealWhenIdleMeta = const VerificationMeta(
    'sealWhenIdle',
  );
  @override
  late final GeneratedColumn<bool> sealWhenIdle = GeneratedColumn<bool>(
    'seal_when_idle',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("seal_when_idle" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _serverSealedMeta = const VerificationMeta(
    'serverSealed',
  );
  @override
  late final GeneratedColumn<bool> serverSealed = GeneratedColumn<bool>(
    'server_sealed',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("server_sealed" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _sealRequestedAtMeta = const VerificationMeta(
    'sealRequestedAt',
  );
  @override
  late final GeneratedColumn<DateTime> sealRequestedAt =
      GeneratedColumn<DateTime>(
        'seal_requested_at',
        aliasedName,
        true,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _sealConfirmedAtMeta = const VerificationMeta(
    'sealConfirmedAt',
  );
  @override
  late final GeneratedColumn<DateTime> sealConfirmedAt =
      GeneratedColumn<DateTime>(
        'seal_confirmed_at',
        aliasedName,
        true,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _sealLeaseOwnerMeta = const VerificationMeta(
    'sealLeaseOwner',
  );
  @override
  late final GeneratedColumn<String> sealLeaseOwner = GeneratedColumn<String>(
    'seal_lease_owner',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _sealLeaseExpiresAtMeta =
      const VerificationMeta('sealLeaseExpiresAt');
  @override
  late final GeneratedColumn<DateTime> sealLeaseExpiresAt =
      GeneratedColumn<DateTime>(
        'seal_lease_expires_at',
        aliasedName,
        true,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _sealRetryCountMeta = const VerificationMeta(
    'sealRetryCount',
  );
  @override
  late final GeneratedColumn<int> sealRetryCount = GeneratedColumn<int>(
    'seal_retry_count',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultValue: const Constant(0),
  );
  static const VerificationMeta _sealNextAttemptAtMeta = const VerificationMeta(
    'sealNextAttemptAt',
  );
  @override
  late final GeneratedColumn<DateTime> sealNextAttemptAt =
      GeneratedColumn<DateTime>(
        'seal_next_attempt_at',
        aliasedName,
        true,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _sealLastErrorCodeMeta = const VerificationMeta(
    'sealLastErrorCode',
  );
  @override
  late final GeneratedColumn<String> sealLastErrorCode =
      GeneratedColumn<String>(
        'seal_last_error_code',
        aliasedName,
        true,
        type: DriftSqlType.string,
        requiredDuringInsert: false,
      );
  @override
  List<GeneratedColumn> get $columns => [
    taskId,
    sealWhenIdle,
    serverSealed,
    sealRequestedAt,
    sealConfirmedAt,
    sealLeaseOwner,
    sealLeaseExpiresAt,
    sealRetryCount,
    sealNextAttemptAt,
    sealLastErrorCode,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'import_queue_tasks';
  @override
  VerificationContext validateIntegrity(
    Insertable<ImportQueueTask> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('task_id')) {
      context.handle(
        _taskIdMeta,
        taskId.isAcceptableOrUnknown(data['task_id']!, _taskIdMeta),
      );
    } else if (isInserting) {
      context.missing(_taskIdMeta);
    }
    if (data.containsKey('seal_when_idle')) {
      context.handle(
        _sealWhenIdleMeta,
        sealWhenIdle.isAcceptableOrUnknown(
          data['seal_when_idle']!,
          _sealWhenIdleMeta,
        ),
      );
    }
    if (data.containsKey('server_sealed')) {
      context.handle(
        _serverSealedMeta,
        serverSealed.isAcceptableOrUnknown(
          data['server_sealed']!,
          _serverSealedMeta,
        ),
      );
    }
    if (data.containsKey('seal_requested_at')) {
      context.handle(
        _sealRequestedAtMeta,
        sealRequestedAt.isAcceptableOrUnknown(
          data['seal_requested_at']!,
          _sealRequestedAtMeta,
        ),
      );
    }
    if (data.containsKey('seal_confirmed_at')) {
      context.handle(
        _sealConfirmedAtMeta,
        sealConfirmedAt.isAcceptableOrUnknown(
          data['seal_confirmed_at']!,
          _sealConfirmedAtMeta,
        ),
      );
    }
    if (data.containsKey('seal_lease_owner')) {
      context.handle(
        _sealLeaseOwnerMeta,
        sealLeaseOwner.isAcceptableOrUnknown(
          data['seal_lease_owner']!,
          _sealLeaseOwnerMeta,
        ),
      );
    }
    if (data.containsKey('seal_lease_expires_at')) {
      context.handle(
        _sealLeaseExpiresAtMeta,
        sealLeaseExpiresAt.isAcceptableOrUnknown(
          data['seal_lease_expires_at']!,
          _sealLeaseExpiresAtMeta,
        ),
      );
    }
    if (data.containsKey('seal_retry_count')) {
      context.handle(
        _sealRetryCountMeta,
        sealRetryCount.isAcceptableOrUnknown(
          data['seal_retry_count']!,
          _sealRetryCountMeta,
        ),
      );
    }
    if (data.containsKey('seal_next_attempt_at')) {
      context.handle(
        _sealNextAttemptAtMeta,
        sealNextAttemptAt.isAcceptableOrUnknown(
          data['seal_next_attempt_at']!,
          _sealNextAttemptAtMeta,
        ),
      );
    }
    if (data.containsKey('seal_last_error_code')) {
      context.handle(
        _sealLastErrorCodeMeta,
        sealLastErrorCode.isAcceptableOrUnknown(
          data['seal_last_error_code']!,
          _sealLastErrorCodeMeta,
        ),
      );
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {taskId};
  @override
  ImportQueueTask map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return ImportQueueTask(
      taskId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}task_id'],
      )!,
      sealWhenIdle: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}seal_when_idle'],
      )!,
      serverSealed: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}server_sealed'],
      )!,
      sealRequestedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}seal_requested_at'],
      ),
      sealConfirmedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}seal_confirmed_at'],
      ),
      sealLeaseOwner: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}seal_lease_owner'],
      ),
      sealLeaseExpiresAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}seal_lease_expires_at'],
      ),
      sealRetryCount: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}seal_retry_count'],
      )!,
      sealNextAttemptAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}seal_next_attempt_at'],
      ),
      sealLastErrorCode: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}seal_last_error_code'],
      ),
    );
  }

  @override
  $ImportQueueTasksTable createAlias(String alias) {
    return $ImportQueueTasksTable(attachedDatabase, alias);
  }
}

class ImportQueueTask extends DataClass implements Insertable<ImportQueueTask> {
  final String taskId;
  final bool sealWhenIdle;
  final bool serverSealed;
  final DateTime? sealRequestedAt;
  final DateTime? sealConfirmedAt;
  final String? sealLeaseOwner;
  final DateTime? sealLeaseExpiresAt;
  final int sealRetryCount;
  final DateTime? sealNextAttemptAt;
  final String? sealLastErrorCode;
  const ImportQueueTask({
    required this.taskId,
    required this.sealWhenIdle,
    required this.serverSealed,
    this.sealRequestedAt,
    this.sealConfirmedAt,
    this.sealLeaseOwner,
    this.sealLeaseExpiresAt,
    required this.sealRetryCount,
    this.sealNextAttemptAt,
    this.sealLastErrorCode,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['task_id'] = Variable<String>(taskId);
    map['seal_when_idle'] = Variable<bool>(sealWhenIdle);
    map['server_sealed'] = Variable<bool>(serverSealed);
    if (!nullToAbsent || sealRequestedAt != null) {
      map['seal_requested_at'] = Variable<DateTime>(sealRequestedAt);
    }
    if (!nullToAbsent || sealConfirmedAt != null) {
      map['seal_confirmed_at'] = Variable<DateTime>(sealConfirmedAt);
    }
    if (!nullToAbsent || sealLeaseOwner != null) {
      map['seal_lease_owner'] = Variable<String>(sealLeaseOwner);
    }
    if (!nullToAbsent || sealLeaseExpiresAt != null) {
      map['seal_lease_expires_at'] = Variable<DateTime>(sealLeaseExpiresAt);
    }
    map['seal_retry_count'] = Variable<int>(sealRetryCount);
    if (!nullToAbsent || sealNextAttemptAt != null) {
      map['seal_next_attempt_at'] = Variable<DateTime>(sealNextAttemptAt);
    }
    if (!nullToAbsent || sealLastErrorCode != null) {
      map['seal_last_error_code'] = Variable<String>(sealLastErrorCode);
    }
    return map;
  }

  ImportQueueTasksCompanion toCompanion(bool nullToAbsent) {
    return ImportQueueTasksCompanion(
      taskId: Value(taskId),
      sealWhenIdle: Value(sealWhenIdle),
      serverSealed: Value(serverSealed),
      sealRequestedAt: sealRequestedAt == null && nullToAbsent
          ? const Value.absent()
          : Value(sealRequestedAt),
      sealConfirmedAt: sealConfirmedAt == null && nullToAbsent
          ? const Value.absent()
          : Value(sealConfirmedAt),
      sealLeaseOwner: sealLeaseOwner == null && nullToAbsent
          ? const Value.absent()
          : Value(sealLeaseOwner),
      sealLeaseExpiresAt: sealLeaseExpiresAt == null && nullToAbsent
          ? const Value.absent()
          : Value(sealLeaseExpiresAt),
      sealRetryCount: Value(sealRetryCount),
      sealNextAttemptAt: sealNextAttemptAt == null && nullToAbsent
          ? const Value.absent()
          : Value(sealNextAttemptAt),
      sealLastErrorCode: sealLastErrorCode == null && nullToAbsent
          ? const Value.absent()
          : Value(sealLastErrorCode),
    );
  }

  factory ImportQueueTask.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return ImportQueueTask(
      taskId: serializer.fromJson<String>(json['taskId']),
      sealWhenIdle: serializer.fromJson<bool>(json['sealWhenIdle']),
      serverSealed: serializer.fromJson<bool>(json['serverSealed']),
      sealRequestedAt: serializer.fromJson<DateTime?>(json['sealRequestedAt']),
      sealConfirmedAt: serializer.fromJson<DateTime?>(json['sealConfirmedAt']),
      sealLeaseOwner: serializer.fromJson<String?>(json['sealLeaseOwner']),
      sealLeaseExpiresAt: serializer.fromJson<DateTime?>(
        json['sealLeaseExpiresAt'],
      ),
      sealRetryCount: serializer.fromJson<int>(json['sealRetryCount']),
      sealNextAttemptAt: serializer.fromJson<DateTime?>(
        json['sealNextAttemptAt'],
      ),
      sealLastErrorCode: serializer.fromJson<String?>(
        json['sealLastErrorCode'],
      ),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'taskId': serializer.toJson<String>(taskId),
      'sealWhenIdle': serializer.toJson<bool>(sealWhenIdle),
      'serverSealed': serializer.toJson<bool>(serverSealed),
      'sealRequestedAt': serializer.toJson<DateTime?>(sealRequestedAt),
      'sealConfirmedAt': serializer.toJson<DateTime?>(sealConfirmedAt),
      'sealLeaseOwner': serializer.toJson<String?>(sealLeaseOwner),
      'sealLeaseExpiresAt': serializer.toJson<DateTime?>(sealLeaseExpiresAt),
      'sealRetryCount': serializer.toJson<int>(sealRetryCount),
      'sealNextAttemptAt': serializer.toJson<DateTime?>(sealNextAttemptAt),
      'sealLastErrorCode': serializer.toJson<String?>(sealLastErrorCode),
    };
  }

  ImportQueueTask copyWith({
    String? taskId,
    bool? sealWhenIdle,
    bool? serverSealed,
    Value<DateTime?> sealRequestedAt = const Value.absent(),
    Value<DateTime?> sealConfirmedAt = const Value.absent(),
    Value<String?> sealLeaseOwner = const Value.absent(),
    Value<DateTime?> sealLeaseExpiresAt = const Value.absent(),
    int? sealRetryCount,
    Value<DateTime?> sealNextAttemptAt = const Value.absent(),
    Value<String?> sealLastErrorCode = const Value.absent(),
  }) => ImportQueueTask(
    taskId: taskId ?? this.taskId,
    sealWhenIdle: sealWhenIdle ?? this.sealWhenIdle,
    serverSealed: serverSealed ?? this.serverSealed,
    sealRequestedAt: sealRequestedAt.present
        ? sealRequestedAt.value
        : this.sealRequestedAt,
    sealConfirmedAt: sealConfirmedAt.present
        ? sealConfirmedAt.value
        : this.sealConfirmedAt,
    sealLeaseOwner: sealLeaseOwner.present
        ? sealLeaseOwner.value
        : this.sealLeaseOwner,
    sealLeaseExpiresAt: sealLeaseExpiresAt.present
        ? sealLeaseExpiresAt.value
        : this.sealLeaseExpiresAt,
    sealRetryCount: sealRetryCount ?? this.sealRetryCount,
    sealNextAttemptAt: sealNextAttemptAt.present
        ? sealNextAttemptAt.value
        : this.sealNextAttemptAt,
    sealLastErrorCode: sealLastErrorCode.present
        ? sealLastErrorCode.value
        : this.sealLastErrorCode,
  );
  ImportQueueTask copyWithCompanion(ImportQueueTasksCompanion data) {
    return ImportQueueTask(
      taskId: data.taskId.present ? data.taskId.value : this.taskId,
      sealWhenIdle: data.sealWhenIdle.present
          ? data.sealWhenIdle.value
          : this.sealWhenIdle,
      serverSealed: data.serverSealed.present
          ? data.serverSealed.value
          : this.serverSealed,
      sealRequestedAt: data.sealRequestedAt.present
          ? data.sealRequestedAt.value
          : this.sealRequestedAt,
      sealConfirmedAt: data.sealConfirmedAt.present
          ? data.sealConfirmedAt.value
          : this.sealConfirmedAt,
      sealLeaseOwner: data.sealLeaseOwner.present
          ? data.sealLeaseOwner.value
          : this.sealLeaseOwner,
      sealLeaseExpiresAt: data.sealLeaseExpiresAt.present
          ? data.sealLeaseExpiresAt.value
          : this.sealLeaseExpiresAt,
      sealRetryCount: data.sealRetryCount.present
          ? data.sealRetryCount.value
          : this.sealRetryCount,
      sealNextAttemptAt: data.sealNextAttemptAt.present
          ? data.sealNextAttemptAt.value
          : this.sealNextAttemptAt,
      sealLastErrorCode: data.sealLastErrorCode.present
          ? data.sealLastErrorCode.value
          : this.sealLastErrorCode,
    );
  }

  @override
  String toString() {
    return (StringBuffer('ImportQueueTask(')
          ..write('taskId: $taskId, ')
          ..write('sealWhenIdle: $sealWhenIdle, ')
          ..write('serverSealed: $serverSealed, ')
          ..write('sealRequestedAt: $sealRequestedAt, ')
          ..write('sealConfirmedAt: $sealConfirmedAt, ')
          ..write('sealLeaseOwner: $sealLeaseOwner, ')
          ..write('sealLeaseExpiresAt: $sealLeaseExpiresAt, ')
          ..write('sealRetryCount: $sealRetryCount, ')
          ..write('sealNextAttemptAt: $sealNextAttemptAt, ')
          ..write('sealLastErrorCode: $sealLastErrorCode')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    taskId,
    sealWhenIdle,
    serverSealed,
    sealRequestedAt,
    sealConfirmedAt,
    sealLeaseOwner,
    sealLeaseExpiresAt,
    sealRetryCount,
    sealNextAttemptAt,
    sealLastErrorCode,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is ImportQueueTask &&
          other.taskId == this.taskId &&
          other.sealWhenIdle == this.sealWhenIdle &&
          other.serverSealed == this.serverSealed &&
          other.sealRequestedAt == this.sealRequestedAt &&
          other.sealConfirmedAt == this.sealConfirmedAt &&
          other.sealLeaseOwner == this.sealLeaseOwner &&
          other.sealLeaseExpiresAt == this.sealLeaseExpiresAt &&
          other.sealRetryCount == this.sealRetryCount &&
          other.sealNextAttemptAt == this.sealNextAttemptAt &&
          other.sealLastErrorCode == this.sealLastErrorCode);
}

class ImportQueueTasksCompanion extends UpdateCompanion<ImportQueueTask> {
  final Value<String> taskId;
  final Value<bool> sealWhenIdle;
  final Value<bool> serverSealed;
  final Value<DateTime?> sealRequestedAt;
  final Value<DateTime?> sealConfirmedAt;
  final Value<String?> sealLeaseOwner;
  final Value<DateTime?> sealLeaseExpiresAt;
  final Value<int> sealRetryCount;
  final Value<DateTime?> sealNextAttemptAt;
  final Value<String?> sealLastErrorCode;
  final Value<int> rowid;
  const ImportQueueTasksCompanion({
    this.taskId = const Value.absent(),
    this.sealWhenIdle = const Value.absent(),
    this.serverSealed = const Value.absent(),
    this.sealRequestedAt = const Value.absent(),
    this.sealConfirmedAt = const Value.absent(),
    this.sealLeaseOwner = const Value.absent(),
    this.sealLeaseExpiresAt = const Value.absent(),
    this.sealRetryCount = const Value.absent(),
    this.sealNextAttemptAt = const Value.absent(),
    this.sealLastErrorCode = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  ImportQueueTasksCompanion.insert({
    required String taskId,
    this.sealWhenIdle = const Value.absent(),
    this.serverSealed = const Value.absent(),
    this.sealRequestedAt = const Value.absent(),
    this.sealConfirmedAt = const Value.absent(),
    this.sealLeaseOwner = const Value.absent(),
    this.sealLeaseExpiresAt = const Value.absent(),
    this.sealRetryCount = const Value.absent(),
    this.sealNextAttemptAt = const Value.absent(),
    this.sealLastErrorCode = const Value.absent(),
    this.rowid = const Value.absent(),
  }) : taskId = Value(taskId);
  static Insertable<ImportQueueTask> custom({
    Expression<String>? taskId,
    Expression<bool>? sealWhenIdle,
    Expression<bool>? serverSealed,
    Expression<DateTime>? sealRequestedAt,
    Expression<DateTime>? sealConfirmedAt,
    Expression<String>? sealLeaseOwner,
    Expression<DateTime>? sealLeaseExpiresAt,
    Expression<int>? sealRetryCount,
    Expression<DateTime>? sealNextAttemptAt,
    Expression<String>? sealLastErrorCode,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (taskId != null) 'task_id': taskId,
      if (sealWhenIdle != null) 'seal_when_idle': sealWhenIdle,
      if (serverSealed != null) 'server_sealed': serverSealed,
      if (sealRequestedAt != null) 'seal_requested_at': sealRequestedAt,
      if (sealConfirmedAt != null) 'seal_confirmed_at': sealConfirmedAt,
      if (sealLeaseOwner != null) 'seal_lease_owner': sealLeaseOwner,
      if (sealLeaseExpiresAt != null)
        'seal_lease_expires_at': sealLeaseExpiresAt,
      if (sealRetryCount != null) 'seal_retry_count': sealRetryCount,
      if (sealNextAttemptAt != null) 'seal_next_attempt_at': sealNextAttemptAt,
      if (sealLastErrorCode != null) 'seal_last_error_code': sealLastErrorCode,
      if (rowid != null) 'rowid': rowid,
    });
  }

  ImportQueueTasksCompanion copyWith({
    Value<String>? taskId,
    Value<bool>? sealWhenIdle,
    Value<bool>? serverSealed,
    Value<DateTime?>? sealRequestedAt,
    Value<DateTime?>? sealConfirmedAt,
    Value<String?>? sealLeaseOwner,
    Value<DateTime?>? sealLeaseExpiresAt,
    Value<int>? sealRetryCount,
    Value<DateTime?>? sealNextAttemptAt,
    Value<String?>? sealLastErrorCode,
    Value<int>? rowid,
  }) {
    return ImportQueueTasksCompanion(
      taskId: taskId ?? this.taskId,
      sealWhenIdle: sealWhenIdle ?? this.sealWhenIdle,
      serverSealed: serverSealed ?? this.serverSealed,
      sealRequestedAt: sealRequestedAt ?? this.sealRequestedAt,
      sealConfirmedAt: sealConfirmedAt ?? this.sealConfirmedAt,
      sealLeaseOwner: sealLeaseOwner ?? this.sealLeaseOwner,
      sealLeaseExpiresAt: sealLeaseExpiresAt ?? this.sealLeaseExpiresAt,
      sealRetryCount: sealRetryCount ?? this.sealRetryCount,
      sealNextAttemptAt: sealNextAttemptAt ?? this.sealNextAttemptAt,
      sealLastErrorCode: sealLastErrorCode ?? this.sealLastErrorCode,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (taskId.present) {
      map['task_id'] = Variable<String>(taskId.value);
    }
    if (sealWhenIdle.present) {
      map['seal_when_idle'] = Variable<bool>(sealWhenIdle.value);
    }
    if (serverSealed.present) {
      map['server_sealed'] = Variable<bool>(serverSealed.value);
    }
    if (sealRequestedAt.present) {
      map['seal_requested_at'] = Variable<DateTime>(sealRequestedAt.value);
    }
    if (sealConfirmedAt.present) {
      map['seal_confirmed_at'] = Variable<DateTime>(sealConfirmedAt.value);
    }
    if (sealLeaseOwner.present) {
      map['seal_lease_owner'] = Variable<String>(sealLeaseOwner.value);
    }
    if (sealLeaseExpiresAt.present) {
      map['seal_lease_expires_at'] = Variable<DateTime>(
        sealLeaseExpiresAt.value,
      );
    }
    if (sealRetryCount.present) {
      map['seal_retry_count'] = Variable<int>(sealRetryCount.value);
    }
    if (sealNextAttemptAt.present) {
      map['seal_next_attempt_at'] = Variable<DateTime>(sealNextAttemptAt.value);
    }
    if (sealLastErrorCode.present) {
      map['seal_last_error_code'] = Variable<String>(sealLastErrorCode.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('ImportQueueTasksCompanion(')
          ..write('taskId: $taskId, ')
          ..write('sealWhenIdle: $sealWhenIdle, ')
          ..write('serverSealed: $serverSealed, ')
          ..write('sealRequestedAt: $sealRequestedAt, ')
          ..write('sealConfirmedAt: $sealConfirmedAt, ')
          ..write('sealLeaseOwner: $sealLeaseOwner, ')
          ..write('sealLeaseExpiresAt: $sealLeaseExpiresAt, ')
          ..write('sealRetryCount: $sealRetryCount, ')
          ..write('sealNextAttemptAt: $sealNextAttemptAt, ')
          ..write('sealLastErrorCode: $sealLastErrorCode, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $ImportQueueAuthoritiesTable extends ImportQueueAuthorities
    with TableInfo<$ImportQueueAuthoritiesTable, ImportQueueAuthority> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $ImportQueueAuthoritiesTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _taskIdMeta = const VerificationMeta('taskId');
  @override
  late final GeneratedColumn<String> taskId = GeneratedColumn<String>(
    'task_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _localAssetIdMeta = const VerificationMeta(
    'localAssetId',
  );
  @override
  late final GeneratedColumn<String> localAssetId = GeneratedColumn<String>(
    'local_asset_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _dispositionMeta = const VerificationMeta(
    'disposition',
  );
  @override
  late final GeneratedColumn<String> disposition = GeneratedColumn<String>(
    'disposition',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [taskId, localAssetId, disposition];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'import_queue_authorities';
  @override
  VerificationContext validateIntegrity(
    Insertable<ImportQueueAuthority> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('task_id')) {
      context.handle(
        _taskIdMeta,
        taskId.isAcceptableOrUnknown(data['task_id']!, _taskIdMeta),
      );
    } else if (isInserting) {
      context.missing(_taskIdMeta);
    }
    if (data.containsKey('local_asset_id')) {
      context.handle(
        _localAssetIdMeta,
        localAssetId.isAcceptableOrUnknown(
          data['local_asset_id']!,
          _localAssetIdMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_localAssetIdMeta);
    }
    if (data.containsKey('disposition')) {
      context.handle(
        _dispositionMeta,
        disposition.isAcceptableOrUnknown(
          data['disposition']!,
          _dispositionMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_dispositionMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {taskId, localAssetId};
  @override
  ImportQueueAuthority map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return ImportQueueAuthority(
      taskId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}task_id'],
      )!,
      localAssetId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}local_asset_id'],
      )!,
      disposition: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}disposition'],
      )!,
    );
  }

  @override
  $ImportQueueAuthoritiesTable createAlias(String alias) {
    return $ImportQueueAuthoritiesTable(attachedDatabase, alias);
  }
}

class ImportQueueAuthority extends DataClass
    implements Insertable<ImportQueueAuthority> {
  final String taskId;
  final String localAssetId;
  final String disposition;
  const ImportQueueAuthority({
    required this.taskId,
    required this.localAssetId,
    required this.disposition,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['task_id'] = Variable<String>(taskId);
    map['local_asset_id'] = Variable<String>(localAssetId);
    map['disposition'] = Variable<String>(disposition);
    return map;
  }

  ImportQueueAuthoritiesCompanion toCompanion(bool nullToAbsent) {
    return ImportQueueAuthoritiesCompanion(
      taskId: Value(taskId),
      localAssetId: Value(localAssetId),
      disposition: Value(disposition),
    );
  }

  factory ImportQueueAuthority.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return ImportQueueAuthority(
      taskId: serializer.fromJson<String>(json['taskId']),
      localAssetId: serializer.fromJson<String>(json['localAssetId']),
      disposition: serializer.fromJson<String>(json['disposition']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'taskId': serializer.toJson<String>(taskId),
      'localAssetId': serializer.toJson<String>(localAssetId),
      'disposition': serializer.toJson<String>(disposition),
    };
  }

  ImportQueueAuthority copyWith({
    String? taskId,
    String? localAssetId,
    String? disposition,
  }) => ImportQueueAuthority(
    taskId: taskId ?? this.taskId,
    localAssetId: localAssetId ?? this.localAssetId,
    disposition: disposition ?? this.disposition,
  );
  ImportQueueAuthority copyWithCompanion(ImportQueueAuthoritiesCompanion data) {
    return ImportQueueAuthority(
      taskId: data.taskId.present ? data.taskId.value : this.taskId,
      localAssetId: data.localAssetId.present
          ? data.localAssetId.value
          : this.localAssetId,
      disposition: data.disposition.present
          ? data.disposition.value
          : this.disposition,
    );
  }

  @override
  String toString() {
    return (StringBuffer('ImportQueueAuthority(')
          ..write('taskId: $taskId, ')
          ..write('localAssetId: $localAssetId, ')
          ..write('disposition: $disposition')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(taskId, localAssetId, disposition);
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is ImportQueueAuthority &&
          other.taskId == this.taskId &&
          other.localAssetId == this.localAssetId &&
          other.disposition == this.disposition);
}

class ImportQueueAuthoritiesCompanion
    extends UpdateCompanion<ImportQueueAuthority> {
  final Value<String> taskId;
  final Value<String> localAssetId;
  final Value<String> disposition;
  final Value<int> rowid;
  const ImportQueueAuthoritiesCompanion({
    this.taskId = const Value.absent(),
    this.localAssetId = const Value.absent(),
    this.disposition = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  ImportQueueAuthoritiesCompanion.insert({
    required String taskId,
    required String localAssetId,
    required String disposition,
    this.rowid = const Value.absent(),
  }) : taskId = Value(taskId),
       localAssetId = Value(localAssetId),
       disposition = Value(disposition);
  static Insertable<ImportQueueAuthority> custom({
    Expression<String>? taskId,
    Expression<String>? localAssetId,
    Expression<String>? disposition,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (taskId != null) 'task_id': taskId,
      if (localAssetId != null) 'local_asset_id': localAssetId,
      if (disposition != null) 'disposition': disposition,
      if (rowid != null) 'rowid': rowid,
    });
  }

  ImportQueueAuthoritiesCompanion copyWith({
    Value<String>? taskId,
    Value<String>? localAssetId,
    Value<String>? disposition,
    Value<int>? rowid,
  }) {
    return ImportQueueAuthoritiesCompanion(
      taskId: taskId ?? this.taskId,
      localAssetId: localAssetId ?? this.localAssetId,
      disposition: disposition ?? this.disposition,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (taskId.present) {
      map['task_id'] = Variable<String>(taskId.value);
    }
    if (localAssetId.present) {
      map['local_asset_id'] = Variable<String>(localAssetId.value);
    }
    if (disposition.present) {
      map['disposition'] = Variable<String>(disposition.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('ImportQueueAuthoritiesCompanion(')
          ..write('taskId: $taskId, ')
          ..write('localAssetId: $localAssetId, ')
          ..write('disposition: $disposition, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $ImportCleanupRefsTable extends ImportCleanupRefs
    with TableInfo<$ImportCleanupRefsTable, ImportCleanupRef> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $ImportCleanupRefsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _taskIdMeta = const VerificationMeta('taskId');
  @override
  late final GeneratedColumn<String> taskId = GeneratedColumn<String>(
    'task_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _locatorMeta = const VerificationMeta(
    'locator',
  );
  @override
  late final GeneratedColumn<String> locator = GeneratedColumn<String>(
    'locator',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _localAssetIdMeta = const VerificationMeta(
    'localAssetId',
  );
  @override
  late final GeneratedColumn<String> localAssetId = GeneratedColumn<String>(
    'local_asset_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _localRefJsonMeta = const VerificationMeta(
    'localRefJson',
  );
  @override
  late final GeneratedColumn<String> localRefJson = GeneratedColumn<String>(
    'local_ref_json',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [
    taskId,
    locator,
    localAssetId,
    localRefJson,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'import_cleanup_refs';
  @override
  VerificationContext validateIntegrity(
    Insertable<ImportCleanupRef> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('task_id')) {
      context.handle(
        _taskIdMeta,
        taskId.isAcceptableOrUnknown(data['task_id']!, _taskIdMeta),
      );
    } else if (isInserting) {
      context.missing(_taskIdMeta);
    }
    if (data.containsKey('locator')) {
      context.handle(
        _locatorMeta,
        locator.isAcceptableOrUnknown(data['locator']!, _locatorMeta),
      );
    } else if (isInserting) {
      context.missing(_locatorMeta);
    }
    if (data.containsKey('local_asset_id')) {
      context.handle(
        _localAssetIdMeta,
        localAssetId.isAcceptableOrUnknown(
          data['local_asset_id']!,
          _localAssetIdMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_localAssetIdMeta);
    }
    if (data.containsKey('local_ref_json')) {
      context.handle(
        _localRefJsonMeta,
        localRefJson.isAcceptableOrUnknown(
          data['local_ref_json']!,
          _localRefJsonMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_localRefJsonMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {taskId, locator};
  @override
  ImportCleanupRef map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return ImportCleanupRef(
      taskId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}task_id'],
      )!,
      locator: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}locator'],
      )!,
      localAssetId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}local_asset_id'],
      )!,
      localRefJson: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}local_ref_json'],
      )!,
    );
  }

  @override
  $ImportCleanupRefsTable createAlias(String alias) {
    return $ImportCleanupRefsTable(attachedDatabase, alias);
  }
}

class ImportCleanupRef extends DataClass
    implements Insertable<ImportCleanupRef> {
  final String taskId;
  final String locator;
  final String localAssetId;
  final String localRefJson;
  const ImportCleanupRef({
    required this.taskId,
    required this.locator,
    required this.localAssetId,
    required this.localRefJson,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['task_id'] = Variable<String>(taskId);
    map['locator'] = Variable<String>(locator);
    map['local_asset_id'] = Variable<String>(localAssetId);
    map['local_ref_json'] = Variable<String>(localRefJson);
    return map;
  }

  ImportCleanupRefsCompanion toCompanion(bool nullToAbsent) {
    return ImportCleanupRefsCompanion(
      taskId: Value(taskId),
      locator: Value(locator),
      localAssetId: Value(localAssetId),
      localRefJson: Value(localRefJson),
    );
  }

  factory ImportCleanupRef.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return ImportCleanupRef(
      taskId: serializer.fromJson<String>(json['taskId']),
      locator: serializer.fromJson<String>(json['locator']),
      localAssetId: serializer.fromJson<String>(json['localAssetId']),
      localRefJson: serializer.fromJson<String>(json['localRefJson']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'taskId': serializer.toJson<String>(taskId),
      'locator': serializer.toJson<String>(locator),
      'localAssetId': serializer.toJson<String>(localAssetId),
      'localRefJson': serializer.toJson<String>(localRefJson),
    };
  }

  ImportCleanupRef copyWith({
    String? taskId,
    String? locator,
    String? localAssetId,
    String? localRefJson,
  }) => ImportCleanupRef(
    taskId: taskId ?? this.taskId,
    locator: locator ?? this.locator,
    localAssetId: localAssetId ?? this.localAssetId,
    localRefJson: localRefJson ?? this.localRefJson,
  );
  ImportCleanupRef copyWithCompanion(ImportCleanupRefsCompanion data) {
    return ImportCleanupRef(
      taskId: data.taskId.present ? data.taskId.value : this.taskId,
      locator: data.locator.present ? data.locator.value : this.locator,
      localAssetId: data.localAssetId.present
          ? data.localAssetId.value
          : this.localAssetId,
      localRefJson: data.localRefJson.present
          ? data.localRefJson.value
          : this.localRefJson,
    );
  }

  @override
  String toString() {
    return (StringBuffer('ImportCleanupRef(')
          ..write('taskId: $taskId, ')
          ..write('locator: $locator, ')
          ..write('localAssetId: $localAssetId, ')
          ..write('localRefJson: $localRefJson')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(taskId, locator, localAssetId, localRefJson);
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is ImportCleanupRef &&
          other.taskId == this.taskId &&
          other.locator == this.locator &&
          other.localAssetId == this.localAssetId &&
          other.localRefJson == this.localRefJson);
}

class ImportCleanupRefsCompanion extends UpdateCompanion<ImportCleanupRef> {
  final Value<String> taskId;
  final Value<String> locator;
  final Value<String> localAssetId;
  final Value<String> localRefJson;
  final Value<int> rowid;
  const ImportCleanupRefsCompanion({
    this.taskId = const Value.absent(),
    this.locator = const Value.absent(),
    this.localAssetId = const Value.absent(),
    this.localRefJson = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  ImportCleanupRefsCompanion.insert({
    required String taskId,
    required String locator,
    required String localAssetId,
    required String localRefJson,
    this.rowid = const Value.absent(),
  }) : taskId = Value(taskId),
       locator = Value(locator),
       localAssetId = Value(localAssetId),
       localRefJson = Value(localRefJson);
  static Insertable<ImportCleanupRef> custom({
    Expression<String>? taskId,
    Expression<String>? locator,
    Expression<String>? localAssetId,
    Expression<String>? localRefJson,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (taskId != null) 'task_id': taskId,
      if (locator != null) 'locator': locator,
      if (localAssetId != null) 'local_asset_id': localAssetId,
      if (localRefJson != null) 'local_ref_json': localRefJson,
      if (rowid != null) 'rowid': rowid,
    });
  }

  ImportCleanupRefsCompanion copyWith({
    Value<String>? taskId,
    Value<String>? locator,
    Value<String>? localAssetId,
    Value<String>? localRefJson,
    Value<int>? rowid,
  }) {
    return ImportCleanupRefsCompanion(
      taskId: taskId ?? this.taskId,
      locator: locator ?? this.locator,
      localAssetId: localAssetId ?? this.localAssetId,
      localRefJson: localRefJson ?? this.localRefJson,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (taskId.present) {
      map['task_id'] = Variable<String>(taskId.value);
    }
    if (locator.present) {
      map['locator'] = Variable<String>(locator.value);
    }
    if (localAssetId.present) {
      map['local_asset_id'] = Variable<String>(localAssetId.value);
    }
    if (localRefJson.present) {
      map['local_ref_json'] = Variable<String>(localRefJson.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('ImportCleanupRefsCompanion(')
          ..write('taskId: $taskId, ')
          ..write('locator: $locator, ')
          ..write('localAssetId: $localAssetId, ')
          ..write('localRefJson: $localRefJson, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

abstract class _$UploadDatabase extends GeneratedDatabase {
  _$UploadDatabase(QueryExecutor e) : super(e);
  $UploadDatabaseManager get managers => $UploadDatabaseManager(this);
  late final $UploadItemsTable uploadItems = $UploadItemsTable(this);
  late final $UploadPartsTable uploadParts = $UploadPartsTable(this);
  late final $ImportQueueTasksTable importQueueTasks = $ImportQueueTasksTable(
    this,
  );
  late final $ImportQueueAuthoritiesTable importQueueAuthorities =
      $ImportQueueAuthoritiesTable(this);
  late final $ImportCleanupRefsTable importCleanupRefs =
      $ImportCleanupRefsTable(this);
  @override
  Iterable<TableInfo<Table, Object?>> get allTables =>
      allSchemaEntities.whereType<TableInfo<Table, Object?>>();
  @override
  List<DatabaseSchemaEntity> get allSchemaEntities => [
    uploadItems,
    uploadParts,
    importQueueTasks,
    importQueueAuthorities,
    importCleanupRefs,
  ];
  @override
  StreamQueryUpdateRules get streamUpdateRules => const StreamQueryUpdateRules([
    WritePropagation(
      on: TableUpdateQuery.onTableName(
        'upload_items',
        limitUpdateKind: UpdateKind.delete,
      ),
      result: [TableUpdate('upload_parts', kind: UpdateKind.delete)],
    ),
  ]);
}

typedef $$UploadItemsTableCreateCompanionBuilder =
    UploadItemsCompanion Function({
      required String localAssetId,
      required String taskId,
      Value<String?> bindingId,
      Value<String?> candidateId,
      required String localRefJson,
      required int byteSize,
      Value<String?> sha256,
      Value<String?> backendAssetId,
      Value<String?> providerUploadId,
      required int partSize,
      Value<int> retryCount,
      Value<DateTime?> nextAttemptAt,
      required String state,
      Value<String?> lastErrorCode,
      Value<String?> lastErrorMessage,
      Value<String?> leaseOwner,
      Value<DateTime?> leaseExpiresAt,
      Value<int> leaseGeneration,
      Value<int> policyGeneration,
      Value<bool> serverConfirmed,
      Value<bool> terminalSummaryAcknowledged,
      Value<bool> retryRequested,
      Value<bool> localRejected,
      Value<String?> pausedFromState,
      Value<int> failureReportRetryCount,
      Value<DateTime?> failureReportNextAttemptAt,
      Value<int> rowid,
    });
typedef $$UploadItemsTableUpdateCompanionBuilder =
    UploadItemsCompanion Function({
      Value<String> localAssetId,
      Value<String> taskId,
      Value<String?> bindingId,
      Value<String?> candidateId,
      Value<String> localRefJson,
      Value<int> byteSize,
      Value<String?> sha256,
      Value<String?> backendAssetId,
      Value<String?> providerUploadId,
      Value<int> partSize,
      Value<int> retryCount,
      Value<DateTime?> nextAttemptAt,
      Value<String> state,
      Value<String?> lastErrorCode,
      Value<String?> lastErrorMessage,
      Value<String?> leaseOwner,
      Value<DateTime?> leaseExpiresAt,
      Value<int> leaseGeneration,
      Value<int> policyGeneration,
      Value<bool> serverConfirmed,
      Value<bool> terminalSummaryAcknowledged,
      Value<bool> retryRequested,
      Value<bool> localRejected,
      Value<String?> pausedFromState,
      Value<int> failureReportRetryCount,
      Value<DateTime?> failureReportNextAttemptAt,
      Value<int> rowid,
    });

final class $$UploadItemsTableReferences
    extends BaseReferences<_$UploadDatabase, $UploadItemsTable, UploadItem> {
  $$UploadItemsTableReferences(super.$_db, super.$_table, super.$_typedResult);

  static MultiTypedResultKey<$UploadPartsTable, List<UploadPart>>
  _uploadPartsRefsTable(_$UploadDatabase db) => MultiTypedResultKey.fromTable(
    db.uploadParts,
    aliasName: 'upload_items__local_asset_id__upload_parts__local_asset_id',
  );

  $$UploadPartsTableProcessedTableManager get uploadPartsRefs {
    final manager = $$UploadPartsTableTableManager($_db, $_db.uploadParts)
        .filter(
          (f) => f.localAssetId.localAssetId.sqlEquals(
            $_itemColumn<String>('local_asset_id')!,
          ),
        );

    final cache = $_typedResult.readTableOrNull(_uploadPartsRefsTable($_db));
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: cache),
    );
  }
}

class $$UploadItemsTableFilterComposer
    extends Composer<_$UploadDatabase, $UploadItemsTable> {
  $$UploadItemsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get localAssetId => $composableBuilder(
    column: $table.localAssetId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get taskId => $composableBuilder(
    column: $table.taskId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get bindingId => $composableBuilder(
    column: $table.bindingId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get candidateId => $composableBuilder(
    column: $table.candidateId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get localRefJson => $composableBuilder(
    column: $table.localRefJson,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get byteSize => $composableBuilder(
    column: $table.byteSize,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get sha256 => $composableBuilder(
    column: $table.sha256,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get backendAssetId => $composableBuilder(
    column: $table.backendAssetId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get providerUploadId => $composableBuilder(
    column: $table.providerUploadId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get partSize => $composableBuilder(
    column: $table.partSize,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get retryCount => $composableBuilder(
    column: $table.retryCount,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get nextAttemptAt => $composableBuilder(
    column: $table.nextAttemptAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get state => $composableBuilder(
    column: $table.state,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get lastErrorCode => $composableBuilder(
    column: $table.lastErrorCode,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get lastErrorMessage => $composableBuilder(
    column: $table.lastErrorMessage,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get leaseOwner => $composableBuilder(
    column: $table.leaseOwner,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get leaseExpiresAt => $composableBuilder(
    column: $table.leaseExpiresAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get leaseGeneration => $composableBuilder(
    column: $table.leaseGeneration,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get policyGeneration => $composableBuilder(
    column: $table.policyGeneration,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get serverConfirmed => $composableBuilder(
    column: $table.serverConfirmed,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get terminalSummaryAcknowledged => $composableBuilder(
    column: $table.terminalSummaryAcknowledged,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get retryRequested => $composableBuilder(
    column: $table.retryRequested,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get localRejected => $composableBuilder(
    column: $table.localRejected,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get pausedFromState => $composableBuilder(
    column: $table.pausedFromState,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get failureReportRetryCount => $composableBuilder(
    column: $table.failureReportRetryCount,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get failureReportNextAttemptAt => $composableBuilder(
    column: $table.failureReportNextAttemptAt,
    builder: (column) => ColumnFilters(column),
  );

  Expression<bool> uploadPartsRefs(
    Expression<bool> Function($$UploadPartsTableFilterComposer f) f,
  ) {
    final $$UploadPartsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.localAssetId,
      referencedTable: $db.uploadParts,
      getReferencedColumn: (t) => t.localAssetId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$UploadPartsTableFilterComposer(
            $db: $db,
            $table: $db.uploadParts,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }
}

class $$UploadItemsTableOrderingComposer
    extends Composer<_$UploadDatabase, $UploadItemsTable> {
  $$UploadItemsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get localAssetId => $composableBuilder(
    column: $table.localAssetId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get taskId => $composableBuilder(
    column: $table.taskId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get bindingId => $composableBuilder(
    column: $table.bindingId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get candidateId => $composableBuilder(
    column: $table.candidateId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get localRefJson => $composableBuilder(
    column: $table.localRefJson,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get byteSize => $composableBuilder(
    column: $table.byteSize,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get sha256 => $composableBuilder(
    column: $table.sha256,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get backendAssetId => $composableBuilder(
    column: $table.backendAssetId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get providerUploadId => $composableBuilder(
    column: $table.providerUploadId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get partSize => $composableBuilder(
    column: $table.partSize,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get retryCount => $composableBuilder(
    column: $table.retryCount,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get nextAttemptAt => $composableBuilder(
    column: $table.nextAttemptAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get state => $composableBuilder(
    column: $table.state,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get lastErrorCode => $composableBuilder(
    column: $table.lastErrorCode,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get lastErrorMessage => $composableBuilder(
    column: $table.lastErrorMessage,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get leaseOwner => $composableBuilder(
    column: $table.leaseOwner,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get leaseExpiresAt => $composableBuilder(
    column: $table.leaseExpiresAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get leaseGeneration => $composableBuilder(
    column: $table.leaseGeneration,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get policyGeneration => $composableBuilder(
    column: $table.policyGeneration,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get serverConfirmed => $composableBuilder(
    column: $table.serverConfirmed,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get terminalSummaryAcknowledged => $composableBuilder(
    column: $table.terminalSummaryAcknowledged,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get retryRequested => $composableBuilder(
    column: $table.retryRequested,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get localRejected => $composableBuilder(
    column: $table.localRejected,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get pausedFromState => $composableBuilder(
    column: $table.pausedFromState,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get failureReportRetryCount => $composableBuilder(
    column: $table.failureReportRetryCount,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get failureReportNextAttemptAt =>
      $composableBuilder(
        column: $table.failureReportNextAttemptAt,
        builder: (column) => ColumnOrderings(column),
      );
}

class $$UploadItemsTableAnnotationComposer
    extends Composer<_$UploadDatabase, $UploadItemsTable> {
  $$UploadItemsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get localAssetId => $composableBuilder(
    column: $table.localAssetId,
    builder: (column) => column,
  );

  GeneratedColumn<String> get taskId =>
      $composableBuilder(column: $table.taskId, builder: (column) => column);

  GeneratedColumn<String> get bindingId =>
      $composableBuilder(column: $table.bindingId, builder: (column) => column);

  GeneratedColumn<String> get candidateId => $composableBuilder(
    column: $table.candidateId,
    builder: (column) => column,
  );

  GeneratedColumn<String> get localRefJson => $composableBuilder(
    column: $table.localRefJson,
    builder: (column) => column,
  );

  GeneratedColumn<int> get byteSize =>
      $composableBuilder(column: $table.byteSize, builder: (column) => column);

  GeneratedColumn<String> get sha256 =>
      $composableBuilder(column: $table.sha256, builder: (column) => column);

  GeneratedColumn<String> get backendAssetId => $composableBuilder(
    column: $table.backendAssetId,
    builder: (column) => column,
  );

  GeneratedColumn<String> get providerUploadId => $composableBuilder(
    column: $table.providerUploadId,
    builder: (column) => column,
  );

  GeneratedColumn<int> get partSize =>
      $composableBuilder(column: $table.partSize, builder: (column) => column);

  GeneratedColumn<int> get retryCount => $composableBuilder(
    column: $table.retryCount,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get nextAttemptAt => $composableBuilder(
    column: $table.nextAttemptAt,
    builder: (column) => column,
  );

  GeneratedColumn<String> get state =>
      $composableBuilder(column: $table.state, builder: (column) => column);

  GeneratedColumn<String> get lastErrorCode => $composableBuilder(
    column: $table.lastErrorCode,
    builder: (column) => column,
  );

  GeneratedColumn<String> get lastErrorMessage => $composableBuilder(
    column: $table.lastErrorMessage,
    builder: (column) => column,
  );

  GeneratedColumn<String> get leaseOwner => $composableBuilder(
    column: $table.leaseOwner,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get leaseExpiresAt => $composableBuilder(
    column: $table.leaseExpiresAt,
    builder: (column) => column,
  );

  GeneratedColumn<int> get leaseGeneration => $composableBuilder(
    column: $table.leaseGeneration,
    builder: (column) => column,
  );

  GeneratedColumn<int> get policyGeneration => $composableBuilder(
    column: $table.policyGeneration,
    builder: (column) => column,
  );

  GeneratedColumn<bool> get serverConfirmed => $composableBuilder(
    column: $table.serverConfirmed,
    builder: (column) => column,
  );

  GeneratedColumn<bool> get terminalSummaryAcknowledged => $composableBuilder(
    column: $table.terminalSummaryAcknowledged,
    builder: (column) => column,
  );

  GeneratedColumn<bool> get retryRequested => $composableBuilder(
    column: $table.retryRequested,
    builder: (column) => column,
  );

  GeneratedColumn<bool> get localRejected => $composableBuilder(
    column: $table.localRejected,
    builder: (column) => column,
  );

  GeneratedColumn<String> get pausedFromState => $composableBuilder(
    column: $table.pausedFromState,
    builder: (column) => column,
  );

  GeneratedColumn<int> get failureReportRetryCount => $composableBuilder(
    column: $table.failureReportRetryCount,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get failureReportNextAttemptAt =>
      $composableBuilder(
        column: $table.failureReportNextAttemptAt,
        builder: (column) => column,
      );

  Expression<T> uploadPartsRefs<T extends Object>(
    Expression<T> Function($$UploadPartsTableAnnotationComposer a) f,
  ) {
    final $$UploadPartsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.localAssetId,
      referencedTable: $db.uploadParts,
      getReferencedColumn: (t) => t.localAssetId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$UploadPartsTableAnnotationComposer(
            $db: $db,
            $table: $db.uploadParts,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }
}

class $$UploadItemsTableTableManager
    extends
        RootTableManager<
          _$UploadDatabase,
          $UploadItemsTable,
          UploadItem,
          $$UploadItemsTableFilterComposer,
          $$UploadItemsTableOrderingComposer,
          $$UploadItemsTableAnnotationComposer,
          $$UploadItemsTableCreateCompanionBuilder,
          $$UploadItemsTableUpdateCompanionBuilder,
          (UploadItem, $$UploadItemsTableReferences),
          UploadItem,
          PrefetchHooks Function({bool uploadPartsRefs})
        > {
  $$UploadItemsTableTableManager(_$UploadDatabase db, $UploadItemsTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$UploadItemsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$UploadItemsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$UploadItemsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> localAssetId = const Value.absent(),
                Value<String> taskId = const Value.absent(),
                Value<String?> bindingId = const Value.absent(),
                Value<String?> candidateId = const Value.absent(),
                Value<String> localRefJson = const Value.absent(),
                Value<int> byteSize = const Value.absent(),
                Value<String?> sha256 = const Value.absent(),
                Value<String?> backendAssetId = const Value.absent(),
                Value<String?> providerUploadId = const Value.absent(),
                Value<int> partSize = const Value.absent(),
                Value<int> retryCount = const Value.absent(),
                Value<DateTime?> nextAttemptAt = const Value.absent(),
                Value<String> state = const Value.absent(),
                Value<String?> lastErrorCode = const Value.absent(),
                Value<String?> lastErrorMessage = const Value.absent(),
                Value<String?> leaseOwner = const Value.absent(),
                Value<DateTime?> leaseExpiresAt = const Value.absent(),
                Value<int> leaseGeneration = const Value.absent(),
                Value<int> policyGeneration = const Value.absent(),
                Value<bool> serverConfirmed = const Value.absent(),
                Value<bool> terminalSummaryAcknowledged = const Value.absent(),
                Value<bool> retryRequested = const Value.absent(),
                Value<bool> localRejected = const Value.absent(),
                Value<String?> pausedFromState = const Value.absent(),
                Value<int> failureReportRetryCount = const Value.absent(),
                Value<DateTime?> failureReportNextAttemptAt =
                    const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => UploadItemsCompanion(
                localAssetId: localAssetId,
                taskId: taskId,
                bindingId: bindingId,
                candidateId: candidateId,
                localRefJson: localRefJson,
                byteSize: byteSize,
                sha256: sha256,
                backendAssetId: backendAssetId,
                providerUploadId: providerUploadId,
                partSize: partSize,
                retryCount: retryCount,
                nextAttemptAt: nextAttemptAt,
                state: state,
                lastErrorCode: lastErrorCode,
                lastErrorMessage: lastErrorMessage,
                leaseOwner: leaseOwner,
                leaseExpiresAt: leaseExpiresAt,
                leaseGeneration: leaseGeneration,
                policyGeneration: policyGeneration,
                serverConfirmed: serverConfirmed,
                terminalSummaryAcknowledged: terminalSummaryAcknowledged,
                retryRequested: retryRequested,
                localRejected: localRejected,
                pausedFromState: pausedFromState,
                failureReportRetryCount: failureReportRetryCount,
                failureReportNextAttemptAt: failureReportNextAttemptAt,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String localAssetId,
                required String taskId,
                Value<String?> bindingId = const Value.absent(),
                Value<String?> candidateId = const Value.absent(),
                required String localRefJson,
                required int byteSize,
                Value<String?> sha256 = const Value.absent(),
                Value<String?> backendAssetId = const Value.absent(),
                Value<String?> providerUploadId = const Value.absent(),
                required int partSize,
                Value<int> retryCount = const Value.absent(),
                Value<DateTime?> nextAttemptAt = const Value.absent(),
                required String state,
                Value<String?> lastErrorCode = const Value.absent(),
                Value<String?> lastErrorMessage = const Value.absent(),
                Value<String?> leaseOwner = const Value.absent(),
                Value<DateTime?> leaseExpiresAt = const Value.absent(),
                Value<int> leaseGeneration = const Value.absent(),
                Value<int> policyGeneration = const Value.absent(),
                Value<bool> serverConfirmed = const Value.absent(),
                Value<bool> terminalSummaryAcknowledged = const Value.absent(),
                Value<bool> retryRequested = const Value.absent(),
                Value<bool> localRejected = const Value.absent(),
                Value<String?> pausedFromState = const Value.absent(),
                Value<int> failureReportRetryCount = const Value.absent(),
                Value<DateTime?> failureReportNextAttemptAt =
                    const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => UploadItemsCompanion.insert(
                localAssetId: localAssetId,
                taskId: taskId,
                bindingId: bindingId,
                candidateId: candidateId,
                localRefJson: localRefJson,
                byteSize: byteSize,
                sha256: sha256,
                backendAssetId: backendAssetId,
                providerUploadId: providerUploadId,
                partSize: partSize,
                retryCount: retryCount,
                nextAttemptAt: nextAttemptAt,
                state: state,
                lastErrorCode: lastErrorCode,
                lastErrorMessage: lastErrorMessage,
                leaseOwner: leaseOwner,
                leaseExpiresAt: leaseExpiresAt,
                leaseGeneration: leaseGeneration,
                policyGeneration: policyGeneration,
                serverConfirmed: serverConfirmed,
                terminalSummaryAcknowledged: terminalSummaryAcknowledged,
                retryRequested: retryRequested,
                localRejected: localRejected,
                pausedFromState: pausedFromState,
                failureReportRetryCount: failureReportRetryCount,
                failureReportNextAttemptAt: failureReportNextAttemptAt,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map(
                (e) => (
                  e.readTable(table),
                  $$UploadItemsTableReferences(db, table, e),
                ),
              )
              .toList(),
          prefetchHooksCallback: ({uploadPartsRefs = false}) {
            return PrefetchHooks(
              db: db,
              explicitlyWatchedTables: [if (uploadPartsRefs) db.uploadParts],
              addJoins: null,
              getPrefetchedDataCallback: (items) async {
                return [
                  if (uploadPartsRefs)
                    await $_getPrefetchedData<
                      UploadItem,
                      $UploadItemsTable,
                      UploadPart
                    >(
                      currentTable: table,
                      referencedTable: $$UploadItemsTableReferences
                          ._uploadPartsRefsTable(db),
                      managerFromTypedResult: (p0) =>
                          $$UploadItemsTableReferences(
                            db,
                            table,
                            p0,
                          ).uploadPartsRefs,
                      referencedItemsForCurrentItem: (item, referencedItems) =>
                          referencedItems.where(
                            (e) => e.localAssetId == item.localAssetId,
                          ),
                      typedResults: items,
                    ),
                ];
              },
            );
          },
        ),
      );
}

typedef $$UploadItemsTableProcessedTableManager =
    ProcessedTableManager<
      _$UploadDatabase,
      $UploadItemsTable,
      UploadItem,
      $$UploadItemsTableFilterComposer,
      $$UploadItemsTableOrderingComposer,
      $$UploadItemsTableAnnotationComposer,
      $$UploadItemsTableCreateCompanionBuilder,
      $$UploadItemsTableUpdateCompanionBuilder,
      (UploadItem, $$UploadItemsTableReferences),
      UploadItem,
      PrefetchHooks Function({bool uploadPartsRefs})
    >;
typedef $$UploadPartsTableCreateCompanionBuilder =
    UploadPartsCompanion Function({
      required String localAssetId,
      required int partNumber,
      Value<String?> etag,
      Value<String?> nativeJobId,
      Value<String?> nativeLeaseOwner,
      Value<int?> nativeLeaseGeneration,
      Value<int?> nativePolicyGeneration,
      Value<int> rowid,
    });
typedef $$UploadPartsTableUpdateCompanionBuilder =
    UploadPartsCompanion Function({
      Value<String> localAssetId,
      Value<int> partNumber,
      Value<String?> etag,
      Value<String?> nativeJobId,
      Value<String?> nativeLeaseOwner,
      Value<int?> nativeLeaseGeneration,
      Value<int?> nativePolicyGeneration,
      Value<int> rowid,
    });

final class $$UploadPartsTableReferences
    extends BaseReferences<_$UploadDatabase, $UploadPartsTable, UploadPart> {
  $$UploadPartsTableReferences(super.$_db, super.$_table, super.$_typedResult);

  static $UploadItemsTable _localAssetIdTable(_$UploadDatabase db) =>
      db.uploadItems.createAlias(
        'upload_parts__local_asset_id__upload_items__local_asset_id',
      );

  $$UploadItemsTableProcessedTableManager get localAssetId {
    final $_column = $_itemColumn<String>('local_asset_id')!;

    final manager = $$UploadItemsTableTableManager(
      $_db,
      $_db.uploadItems,
    ).filter((f) => f.localAssetId.sqlEquals($_column));
    final item = $_typedResult.readTableOrNull(_localAssetIdTable($_db));
    if (item == null) return manager;
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: [item]),
    );
  }
}

class $$UploadPartsTableFilterComposer
    extends Composer<_$UploadDatabase, $UploadPartsTable> {
  $$UploadPartsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<int> get partNumber => $composableBuilder(
    column: $table.partNumber,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get etag => $composableBuilder(
    column: $table.etag,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get nativeJobId => $composableBuilder(
    column: $table.nativeJobId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get nativeLeaseOwner => $composableBuilder(
    column: $table.nativeLeaseOwner,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get nativeLeaseGeneration => $composableBuilder(
    column: $table.nativeLeaseGeneration,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get nativePolicyGeneration => $composableBuilder(
    column: $table.nativePolicyGeneration,
    builder: (column) => ColumnFilters(column),
  );

  $$UploadItemsTableFilterComposer get localAssetId {
    final $$UploadItemsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.localAssetId,
      referencedTable: $db.uploadItems,
      getReferencedColumn: (t) => t.localAssetId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$UploadItemsTableFilterComposer(
            $db: $db,
            $table: $db.uploadItems,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$UploadPartsTableOrderingComposer
    extends Composer<_$UploadDatabase, $UploadPartsTable> {
  $$UploadPartsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<int> get partNumber => $composableBuilder(
    column: $table.partNumber,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get etag => $composableBuilder(
    column: $table.etag,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get nativeJobId => $composableBuilder(
    column: $table.nativeJobId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get nativeLeaseOwner => $composableBuilder(
    column: $table.nativeLeaseOwner,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get nativeLeaseGeneration => $composableBuilder(
    column: $table.nativeLeaseGeneration,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get nativePolicyGeneration => $composableBuilder(
    column: $table.nativePolicyGeneration,
    builder: (column) => ColumnOrderings(column),
  );

  $$UploadItemsTableOrderingComposer get localAssetId {
    final $$UploadItemsTableOrderingComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.localAssetId,
      referencedTable: $db.uploadItems,
      getReferencedColumn: (t) => t.localAssetId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$UploadItemsTableOrderingComposer(
            $db: $db,
            $table: $db.uploadItems,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$UploadPartsTableAnnotationComposer
    extends Composer<_$UploadDatabase, $UploadPartsTable> {
  $$UploadPartsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<int> get partNumber => $composableBuilder(
    column: $table.partNumber,
    builder: (column) => column,
  );

  GeneratedColumn<String> get etag =>
      $composableBuilder(column: $table.etag, builder: (column) => column);

  GeneratedColumn<String> get nativeJobId => $composableBuilder(
    column: $table.nativeJobId,
    builder: (column) => column,
  );

  GeneratedColumn<String> get nativeLeaseOwner => $composableBuilder(
    column: $table.nativeLeaseOwner,
    builder: (column) => column,
  );

  GeneratedColumn<int> get nativeLeaseGeneration => $composableBuilder(
    column: $table.nativeLeaseGeneration,
    builder: (column) => column,
  );

  GeneratedColumn<int> get nativePolicyGeneration => $composableBuilder(
    column: $table.nativePolicyGeneration,
    builder: (column) => column,
  );

  $$UploadItemsTableAnnotationComposer get localAssetId {
    final $$UploadItemsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.localAssetId,
      referencedTable: $db.uploadItems,
      getReferencedColumn: (t) => t.localAssetId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$UploadItemsTableAnnotationComposer(
            $db: $db,
            $table: $db.uploadItems,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$UploadPartsTableTableManager
    extends
        RootTableManager<
          _$UploadDatabase,
          $UploadPartsTable,
          UploadPart,
          $$UploadPartsTableFilterComposer,
          $$UploadPartsTableOrderingComposer,
          $$UploadPartsTableAnnotationComposer,
          $$UploadPartsTableCreateCompanionBuilder,
          $$UploadPartsTableUpdateCompanionBuilder,
          (UploadPart, $$UploadPartsTableReferences),
          UploadPart,
          PrefetchHooks Function({bool localAssetId})
        > {
  $$UploadPartsTableTableManager(_$UploadDatabase db, $UploadPartsTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$UploadPartsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$UploadPartsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$UploadPartsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> localAssetId = const Value.absent(),
                Value<int> partNumber = const Value.absent(),
                Value<String?> etag = const Value.absent(),
                Value<String?> nativeJobId = const Value.absent(),
                Value<String?> nativeLeaseOwner = const Value.absent(),
                Value<int?> nativeLeaseGeneration = const Value.absent(),
                Value<int?> nativePolicyGeneration = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => UploadPartsCompanion(
                localAssetId: localAssetId,
                partNumber: partNumber,
                etag: etag,
                nativeJobId: nativeJobId,
                nativeLeaseOwner: nativeLeaseOwner,
                nativeLeaseGeneration: nativeLeaseGeneration,
                nativePolicyGeneration: nativePolicyGeneration,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String localAssetId,
                required int partNumber,
                Value<String?> etag = const Value.absent(),
                Value<String?> nativeJobId = const Value.absent(),
                Value<String?> nativeLeaseOwner = const Value.absent(),
                Value<int?> nativeLeaseGeneration = const Value.absent(),
                Value<int?> nativePolicyGeneration = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => UploadPartsCompanion.insert(
                localAssetId: localAssetId,
                partNumber: partNumber,
                etag: etag,
                nativeJobId: nativeJobId,
                nativeLeaseOwner: nativeLeaseOwner,
                nativeLeaseGeneration: nativeLeaseGeneration,
                nativePolicyGeneration: nativePolicyGeneration,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map(
                (e) => (
                  e.readTable(table),
                  $$UploadPartsTableReferences(db, table, e),
                ),
              )
              .toList(),
          prefetchHooksCallback: ({localAssetId = false}) {
            return PrefetchHooks(
              db: db,
              explicitlyWatchedTables: [],
              addJoins:
                  <
                    T extends TableManagerState<
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic
                    >
                  >(state) {
                    if (localAssetId) {
                      state = state.withJoin(
                        currentTable: table,
                        currentColumn: table.localAssetId,
                        referencedTable: $$UploadPartsTableReferences
                            ._localAssetIdTable(db),
                        referencedColumn: $$UploadPartsTableReferences
                            ._localAssetIdTable(db)
                            .localAssetId,
                      ) as T;
                    }

                    return state;
                  },
              getPrefetchedDataCallback: (items) async {
                return [];
              },
            );
          },
        ),
      );
}

typedef $$UploadPartsTableProcessedTableManager =
    ProcessedTableManager<
      _$UploadDatabase,
      $UploadPartsTable,
      UploadPart,
      $$UploadPartsTableFilterComposer,
      $$UploadPartsTableOrderingComposer,
      $$UploadPartsTableAnnotationComposer,
      $$UploadPartsTableCreateCompanionBuilder,
      $$UploadPartsTableUpdateCompanionBuilder,
      (UploadPart, $$UploadPartsTableReferences),
      UploadPart,
      PrefetchHooks Function({bool localAssetId})
    >;
typedef $$ImportQueueTasksTableCreateCompanionBuilder =
    ImportQueueTasksCompanion Function({
      required String taskId,
      Value<bool> sealWhenIdle,
      Value<bool> serverSealed,
      Value<DateTime?> sealRequestedAt,
      Value<DateTime?> sealConfirmedAt,
      Value<String?> sealLeaseOwner,
      Value<DateTime?> sealLeaseExpiresAt,
      Value<int> sealRetryCount,
      Value<DateTime?> sealNextAttemptAt,
      Value<String?> sealLastErrorCode,
      Value<int> rowid,
    });
typedef $$ImportQueueTasksTableUpdateCompanionBuilder =
    ImportQueueTasksCompanion Function({
      Value<String> taskId,
      Value<bool> sealWhenIdle,
      Value<bool> serverSealed,
      Value<DateTime?> sealRequestedAt,
      Value<DateTime?> sealConfirmedAt,
      Value<String?> sealLeaseOwner,
      Value<DateTime?> sealLeaseExpiresAt,
      Value<int> sealRetryCount,
      Value<DateTime?> sealNextAttemptAt,
      Value<String?> sealLastErrorCode,
      Value<int> rowid,
    });

class $$ImportQueueTasksTableFilterComposer
    extends Composer<_$UploadDatabase, $ImportQueueTasksTable> {
  $$ImportQueueTasksTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get taskId => $composableBuilder(
    column: $table.taskId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get sealWhenIdle => $composableBuilder(
    column: $table.sealWhenIdle,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get serverSealed => $composableBuilder(
    column: $table.serverSealed,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get sealRequestedAt => $composableBuilder(
    column: $table.sealRequestedAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get sealConfirmedAt => $composableBuilder(
    column: $table.sealConfirmedAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get sealLeaseOwner => $composableBuilder(
    column: $table.sealLeaseOwner,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get sealLeaseExpiresAt => $composableBuilder(
    column: $table.sealLeaseExpiresAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get sealRetryCount => $composableBuilder(
    column: $table.sealRetryCount,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get sealNextAttemptAt => $composableBuilder(
    column: $table.sealNextAttemptAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get sealLastErrorCode => $composableBuilder(
    column: $table.sealLastErrorCode,
    builder: (column) => ColumnFilters(column),
  );
}

class $$ImportQueueTasksTableOrderingComposer
    extends Composer<_$UploadDatabase, $ImportQueueTasksTable> {
  $$ImportQueueTasksTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get taskId => $composableBuilder(
    column: $table.taskId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get sealWhenIdle => $composableBuilder(
    column: $table.sealWhenIdle,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get serverSealed => $composableBuilder(
    column: $table.serverSealed,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get sealRequestedAt => $composableBuilder(
    column: $table.sealRequestedAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get sealConfirmedAt => $composableBuilder(
    column: $table.sealConfirmedAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get sealLeaseOwner => $composableBuilder(
    column: $table.sealLeaseOwner,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get sealLeaseExpiresAt => $composableBuilder(
    column: $table.sealLeaseExpiresAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get sealRetryCount => $composableBuilder(
    column: $table.sealRetryCount,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get sealNextAttemptAt => $composableBuilder(
    column: $table.sealNextAttemptAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get sealLastErrorCode => $composableBuilder(
    column: $table.sealLastErrorCode,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$ImportQueueTasksTableAnnotationComposer
    extends Composer<_$UploadDatabase, $ImportQueueTasksTable> {
  $$ImportQueueTasksTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get taskId =>
      $composableBuilder(column: $table.taskId, builder: (column) => column);

  GeneratedColumn<bool> get sealWhenIdle => $composableBuilder(
    column: $table.sealWhenIdle,
    builder: (column) => column,
  );

  GeneratedColumn<bool> get serverSealed => $composableBuilder(
    column: $table.serverSealed,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get sealRequestedAt => $composableBuilder(
    column: $table.sealRequestedAt,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get sealConfirmedAt => $composableBuilder(
    column: $table.sealConfirmedAt,
    builder: (column) => column,
  );

  GeneratedColumn<String> get sealLeaseOwner => $composableBuilder(
    column: $table.sealLeaseOwner,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get sealLeaseExpiresAt => $composableBuilder(
    column: $table.sealLeaseExpiresAt,
    builder: (column) => column,
  );

  GeneratedColumn<int> get sealRetryCount => $composableBuilder(
    column: $table.sealRetryCount,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get sealNextAttemptAt => $composableBuilder(
    column: $table.sealNextAttemptAt,
    builder: (column) => column,
  );

  GeneratedColumn<String> get sealLastErrorCode => $composableBuilder(
    column: $table.sealLastErrorCode,
    builder: (column) => column,
  );
}

class $$ImportQueueTasksTableTableManager
    extends
        RootTableManager<
          _$UploadDatabase,
          $ImportQueueTasksTable,
          ImportQueueTask,
          $$ImportQueueTasksTableFilterComposer,
          $$ImportQueueTasksTableOrderingComposer,
          $$ImportQueueTasksTableAnnotationComposer,
          $$ImportQueueTasksTableCreateCompanionBuilder,
          $$ImportQueueTasksTableUpdateCompanionBuilder,
          (
            ImportQueueTask,
            BaseReferences<
              _$UploadDatabase,
              $ImportQueueTasksTable,
              ImportQueueTask
            >,
          ),
          ImportQueueTask,
          PrefetchHooks Function()
        > {
  $$ImportQueueTasksTableTableManager(
    _$UploadDatabase db,
    $ImportQueueTasksTable table,
  ) : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$ImportQueueTasksTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$ImportQueueTasksTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$ImportQueueTasksTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> taskId = const Value.absent(),
                Value<bool> sealWhenIdle = const Value.absent(),
                Value<bool> serverSealed = const Value.absent(),
                Value<DateTime?> sealRequestedAt = const Value.absent(),
                Value<DateTime?> sealConfirmedAt = const Value.absent(),
                Value<String?> sealLeaseOwner = const Value.absent(),
                Value<DateTime?> sealLeaseExpiresAt = const Value.absent(),
                Value<int> sealRetryCount = const Value.absent(),
                Value<DateTime?> sealNextAttemptAt = const Value.absent(),
                Value<String?> sealLastErrorCode = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => ImportQueueTasksCompanion(
                taskId: taskId,
                sealWhenIdle: sealWhenIdle,
                serverSealed: serverSealed,
                sealRequestedAt: sealRequestedAt,
                sealConfirmedAt: sealConfirmedAt,
                sealLeaseOwner: sealLeaseOwner,
                sealLeaseExpiresAt: sealLeaseExpiresAt,
                sealRetryCount: sealRetryCount,
                sealNextAttemptAt: sealNextAttemptAt,
                sealLastErrorCode: sealLastErrorCode,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String taskId,
                Value<bool> sealWhenIdle = const Value.absent(),
                Value<bool> serverSealed = const Value.absent(),
                Value<DateTime?> sealRequestedAt = const Value.absent(),
                Value<DateTime?> sealConfirmedAt = const Value.absent(),
                Value<String?> sealLeaseOwner = const Value.absent(),
                Value<DateTime?> sealLeaseExpiresAt = const Value.absent(),
                Value<int> sealRetryCount = const Value.absent(),
                Value<DateTime?> sealNextAttemptAt = const Value.absent(),
                Value<String?> sealLastErrorCode = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => ImportQueueTasksCompanion.insert(
                taskId: taskId,
                sealWhenIdle: sealWhenIdle,
                serverSealed: serverSealed,
                sealRequestedAt: sealRequestedAt,
                sealConfirmedAt: sealConfirmedAt,
                sealLeaseOwner: sealLeaseOwner,
                sealLeaseExpiresAt: sealLeaseExpiresAt,
                sealRetryCount: sealRetryCount,
                sealNextAttemptAt: sealNextAttemptAt,
                sealLastErrorCode: sealLastErrorCode,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$ImportQueueTasksTableProcessedTableManager =
    ProcessedTableManager<
      _$UploadDatabase,
      $ImportQueueTasksTable,
      ImportQueueTask,
      $$ImportQueueTasksTableFilterComposer,
      $$ImportQueueTasksTableOrderingComposer,
      $$ImportQueueTasksTableAnnotationComposer,
      $$ImportQueueTasksTableCreateCompanionBuilder,
      $$ImportQueueTasksTableUpdateCompanionBuilder,
      (
        ImportQueueTask,
        BaseReferences<
          _$UploadDatabase,
          $ImportQueueTasksTable,
          ImportQueueTask
        >,
      ),
      ImportQueueTask,
      PrefetchHooks Function()
    >;
typedef $$ImportQueueAuthoritiesTableCreateCompanionBuilder =
    ImportQueueAuthoritiesCompanion Function({
      required String taskId,
      required String localAssetId,
      required String disposition,
      Value<int> rowid,
    });
typedef $$ImportQueueAuthoritiesTableUpdateCompanionBuilder =
    ImportQueueAuthoritiesCompanion Function({
      Value<String> taskId,
      Value<String> localAssetId,
      Value<String> disposition,
      Value<int> rowid,
    });

class $$ImportQueueAuthoritiesTableFilterComposer
    extends Composer<_$UploadDatabase, $ImportQueueAuthoritiesTable> {
  $$ImportQueueAuthoritiesTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get taskId => $composableBuilder(
    column: $table.taskId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get localAssetId => $composableBuilder(
    column: $table.localAssetId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get disposition => $composableBuilder(
    column: $table.disposition,
    builder: (column) => ColumnFilters(column),
  );
}

class $$ImportQueueAuthoritiesTableOrderingComposer
    extends Composer<_$UploadDatabase, $ImportQueueAuthoritiesTable> {
  $$ImportQueueAuthoritiesTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get taskId => $composableBuilder(
    column: $table.taskId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get localAssetId => $composableBuilder(
    column: $table.localAssetId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get disposition => $composableBuilder(
    column: $table.disposition,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$ImportQueueAuthoritiesTableAnnotationComposer
    extends Composer<_$UploadDatabase, $ImportQueueAuthoritiesTable> {
  $$ImportQueueAuthoritiesTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get taskId =>
      $composableBuilder(column: $table.taskId, builder: (column) => column);

  GeneratedColumn<String> get localAssetId => $composableBuilder(
    column: $table.localAssetId,
    builder: (column) => column,
  );

  GeneratedColumn<String> get disposition => $composableBuilder(
    column: $table.disposition,
    builder: (column) => column,
  );
}

class $$ImportQueueAuthoritiesTableTableManager
    extends
        RootTableManager<
          _$UploadDatabase,
          $ImportQueueAuthoritiesTable,
          ImportQueueAuthority,
          $$ImportQueueAuthoritiesTableFilterComposer,
          $$ImportQueueAuthoritiesTableOrderingComposer,
          $$ImportQueueAuthoritiesTableAnnotationComposer,
          $$ImportQueueAuthoritiesTableCreateCompanionBuilder,
          $$ImportQueueAuthoritiesTableUpdateCompanionBuilder,
          (
            ImportQueueAuthority,
            BaseReferences<
              _$UploadDatabase,
              $ImportQueueAuthoritiesTable,
              ImportQueueAuthority
            >,
          ),
          ImportQueueAuthority,
          PrefetchHooks Function()
        > {
  $$ImportQueueAuthoritiesTableTableManager(
    _$UploadDatabase db,
    $ImportQueueAuthoritiesTable table,
  ) : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$ImportQueueAuthoritiesTableFilterComposer(
                $db: db,
                $table: table,
              ),
          createOrderingComposer: () =>
              $$ImportQueueAuthoritiesTableOrderingComposer(
                $db: db,
                $table: table,
              ),
          createComputedFieldComposer: () =>
              $$ImportQueueAuthoritiesTableAnnotationComposer(
                $db: db,
                $table: table,
              ),
          updateCompanionCallback:
              ({
                Value<String> taskId = const Value.absent(),
                Value<String> localAssetId = const Value.absent(),
                Value<String> disposition = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => ImportQueueAuthoritiesCompanion(
                taskId: taskId,
                localAssetId: localAssetId,
                disposition: disposition,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String taskId,
                required String localAssetId,
                required String disposition,
                Value<int> rowid = const Value.absent(),
              }) => ImportQueueAuthoritiesCompanion.insert(
                taskId: taskId,
                localAssetId: localAssetId,
                disposition: disposition,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$ImportQueueAuthoritiesTableProcessedTableManager =
    ProcessedTableManager<
      _$UploadDatabase,
      $ImportQueueAuthoritiesTable,
      ImportQueueAuthority,
      $$ImportQueueAuthoritiesTableFilterComposer,
      $$ImportQueueAuthoritiesTableOrderingComposer,
      $$ImportQueueAuthoritiesTableAnnotationComposer,
      $$ImportQueueAuthoritiesTableCreateCompanionBuilder,
      $$ImportQueueAuthoritiesTableUpdateCompanionBuilder,
      (
        ImportQueueAuthority,
        BaseReferences<
          _$UploadDatabase,
          $ImportQueueAuthoritiesTable,
          ImportQueueAuthority
        >,
      ),
      ImportQueueAuthority,
      PrefetchHooks Function()
    >;
typedef $$ImportCleanupRefsTableCreateCompanionBuilder =
    ImportCleanupRefsCompanion Function({
      required String taskId,
      required String locator,
      required String localAssetId,
      required String localRefJson,
      Value<int> rowid,
    });
typedef $$ImportCleanupRefsTableUpdateCompanionBuilder =
    ImportCleanupRefsCompanion Function({
      Value<String> taskId,
      Value<String> locator,
      Value<String> localAssetId,
      Value<String> localRefJson,
      Value<int> rowid,
    });

class $$ImportCleanupRefsTableFilterComposer
    extends Composer<_$UploadDatabase, $ImportCleanupRefsTable> {
  $$ImportCleanupRefsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get taskId => $composableBuilder(
    column: $table.taskId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get locator => $composableBuilder(
    column: $table.locator,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get localAssetId => $composableBuilder(
    column: $table.localAssetId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get localRefJson => $composableBuilder(
    column: $table.localRefJson,
    builder: (column) => ColumnFilters(column),
  );
}

class $$ImportCleanupRefsTableOrderingComposer
    extends Composer<_$UploadDatabase, $ImportCleanupRefsTable> {
  $$ImportCleanupRefsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get taskId => $composableBuilder(
    column: $table.taskId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get locator => $composableBuilder(
    column: $table.locator,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get localAssetId => $composableBuilder(
    column: $table.localAssetId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get localRefJson => $composableBuilder(
    column: $table.localRefJson,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$ImportCleanupRefsTableAnnotationComposer
    extends Composer<_$UploadDatabase, $ImportCleanupRefsTable> {
  $$ImportCleanupRefsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get taskId =>
      $composableBuilder(column: $table.taskId, builder: (column) => column);

  GeneratedColumn<String> get locator =>
      $composableBuilder(column: $table.locator, builder: (column) => column);

  GeneratedColumn<String> get localAssetId => $composableBuilder(
    column: $table.localAssetId,
    builder: (column) => column,
  );

  GeneratedColumn<String> get localRefJson => $composableBuilder(
    column: $table.localRefJson,
    builder: (column) => column,
  );
}

class $$ImportCleanupRefsTableTableManager
    extends
        RootTableManager<
          _$UploadDatabase,
          $ImportCleanupRefsTable,
          ImportCleanupRef,
          $$ImportCleanupRefsTableFilterComposer,
          $$ImportCleanupRefsTableOrderingComposer,
          $$ImportCleanupRefsTableAnnotationComposer,
          $$ImportCleanupRefsTableCreateCompanionBuilder,
          $$ImportCleanupRefsTableUpdateCompanionBuilder,
          (
            ImportCleanupRef,
            BaseReferences<
              _$UploadDatabase,
              $ImportCleanupRefsTable,
              ImportCleanupRef
            >,
          ),
          ImportCleanupRef,
          PrefetchHooks Function()
        > {
  $$ImportCleanupRefsTableTableManager(
    _$UploadDatabase db,
    $ImportCleanupRefsTable table,
  ) : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$ImportCleanupRefsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$ImportCleanupRefsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$ImportCleanupRefsTableAnnotationComposer(
                $db: db,
                $table: table,
              ),
          updateCompanionCallback:
              ({
                Value<String> taskId = const Value.absent(),
                Value<String> locator = const Value.absent(),
                Value<String> localAssetId = const Value.absent(),
                Value<String> localRefJson = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => ImportCleanupRefsCompanion(
                taskId: taskId,
                locator: locator,
                localAssetId: localAssetId,
                localRefJson: localRefJson,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String taskId,
                required String locator,
                required String localAssetId,
                required String localRefJson,
                Value<int> rowid = const Value.absent(),
              }) => ImportCleanupRefsCompanion.insert(
                taskId: taskId,
                locator: locator,
                localAssetId: localAssetId,
                localRefJson: localRefJson,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$ImportCleanupRefsTableProcessedTableManager =
    ProcessedTableManager<
      _$UploadDatabase,
      $ImportCleanupRefsTable,
      ImportCleanupRef,
      $$ImportCleanupRefsTableFilterComposer,
      $$ImportCleanupRefsTableOrderingComposer,
      $$ImportCleanupRefsTableAnnotationComposer,
      $$ImportCleanupRefsTableCreateCompanionBuilder,
      $$ImportCleanupRefsTableUpdateCompanionBuilder,
      (
        ImportCleanupRef,
        BaseReferences<
          _$UploadDatabase,
          $ImportCleanupRefsTable,
          ImportCleanupRef
        >,
      ),
      ImportCleanupRef,
      PrefetchHooks Function()
    >;

class $UploadDatabaseManager {
  final _$UploadDatabase _db;
  $UploadDatabaseManager(this._db);
  $$UploadItemsTableTableManager get uploadItems =>
      $$UploadItemsTableTableManager(_db, _db.uploadItems);
  $$UploadPartsTableTableManager get uploadParts =>
      $$UploadPartsTableTableManager(_db, _db.uploadParts);
  $$ImportQueueTasksTableTableManager get importQueueTasks =>
      $$ImportQueueTasksTableTableManager(_db, _db.importQueueTasks);
  $$ImportQueueAuthoritiesTableTableManager get importQueueAuthorities =>
      $$ImportQueueAuthoritiesTableTableManager(
        _db,
        _db.importQueueAuthorities,
      );
  $$ImportCleanupRefsTableTableManager get importCleanupRefs =>
      $$ImportCleanupRefsTableTableManager(_db, _db.importCleanupRefs);
}
