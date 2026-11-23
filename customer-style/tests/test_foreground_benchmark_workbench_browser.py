"""Browser contract for the private foreground benchmark workbench."""

from opc_style.web.style_suite_page import STYLE_SUITE_PAGE


def test_workbench_has_local_intake_progress_review_and_freeze_controls() -> None:
    for fragment in (
        'id="benchmark-workbench"',
        "真实验收数据集工作台",
        'id="benchmark-calibration-count"',
        "0 / 10",
        'id="benchmark-acceptance-count"',
        "0 / 50",
        'id="benchmark-files" type="file" multiple',
        'id="benchmark-consent"',
        'id="benchmark-split"',
        'id="benchmark-state-filter"',
        'id="benchmark-blockers"',
        'id="benchmark-pilot-status"',
        "小样本技术试跑",
        "不等于正式质量验收通过",
        "p.pilot_ready",
        'id="benchmark-freeze-confirm"',
        'id="benchmark-freeze"',
        "生成机器遮罩建议",
        "人工校对金标",
        "确认人工金标",
    ):
        assert fragment in STYLE_SUITE_PAGE


def test_workbench_batch_stops_after_failure_and_keeps_following_items_pending() -> None:
    assert "benchmarkUploadSequentially" in STYLE_SUITE_PAGE
    assert "items[index].state='failed'" in STYLE_SUITE_PAGE
    assert "break" in STYLE_SUITE_PAGE
    assert "待重试" in STYLE_SUITE_PAGE
    assert "单张失败不会撤销已成功图片" in STYLE_SUITE_PAGE


def test_workbench_does_not_add_external_assets_or_store_private_bytes() -> None:
    assert "Git 忽略的本地私有目录" in STYLE_SUITE_PAGE
    assert "仓库外的本地私有目录" not in STYLE_SUITE_PAGE
    assert "benchmarkDatasetKey" in STYLE_SUITE_PAGE
    assert "localStorage.setItem(benchmarkDatasetKey" in STYLE_SUITE_PAGE
    assert "localStorage.setItem('benchmark-source" not in STYLE_SUITE_PAGE
    assert "localStorage.setItem('benchmark-mask" not in STYLE_SUITE_PAGE
    assert '<script src="http' not in STYLE_SUITE_PAGE
    assert '<img src="http' not in STYLE_SUITE_PAGE


def test_workbench_reuses_canvas_editor_for_gold_mask_instead_of_uploading_it() -> None:
    assert "benchmarkOpenMaskEditor" in STYLE_SUITE_PAGE
    assert "openMaskEditorForTarget" in STYLE_SUITE_PAGE
    assert "workbench-gold" in STYLE_SUITE_PAGE
    assert "picker.files[0]" not in STYLE_SUITE_PAGE
    assert "form.append('gold_mask',blob,'gold-mask.png')" in STYLE_SUITE_PAGE
    assert "低分辨率：只验边界，不验商品清晰度" in STYLE_SUITE_PAGE
    assert 'id="mask-component-files"' not in STYLE_SUITE_PAGE
    assert "componentBlobs" in STYLE_SUITE_PAGE


def test_workbench_can_start_blind_gold_from_a_blank_mask() -> None:
    for fragment in (
        "从空白开始画人工金标（盲测）",
        "data-benchmark-blank-gold",
        "benchmarkOpenBlankMaskEditor",
        "maskUrl:null",
        "baseMaskSha256:'blank'",
        "human-mask-editor-v2:blind-gold",
        "未读取任何机器候选",
        "maskBrushSize.value='40'",
        "maskReset.textContent='清空重画'",
    ):
        assert fragment in STYLE_SUITE_PAGE
    blank_flow = STYLE_SUITE_PAGE.split(
        "async function benchmarkOpenBlankMaskEditor", 1
    )[1].split("async function benchmarkOpenMaskEditor", 1)[0]
    assert "/proposal" not in blank_flow
    assert "benchmarkPropose" not in blank_flow


def test_workbench_prefers_refined_mask_and_keeps_coarse_mask_as_an_aid() -> None:
    assert "校对技术候选（待人工确认）" in STYLE_SUITE_PAGE
    assert "校对后级遮罩（推荐）" in STYLE_SUITE_PAGE
    assert "查看框选范围（辅助）" in STYLE_SUITE_PAGE
    assert "技术校准候选：可作为人工修边起点" in STYLE_SUITE_PAGE
    assert "后级遮罩可作为人工修边起点；框选范围只作辅助" in STYLE_SUITE_PAGE
    assert "data-benchmark-coarse" in STYLE_SUITE_PAGE
    assert "kind=${maskKind}" in STYLE_SUITE_PAGE
    assert "preferredKind==='coarse'&&sample.has_coarse_mask?'coarse'" in STYLE_SUITE_PAGE
    assert "benchmarkOpenMaskEditor(sample,'coarse')" in STYLE_SUITE_PAGE
    assert "自动确认框选候选" not in STYLE_SUITE_PAGE


def test_workbench_triages_candidates_and_requires_local_correction_for_risk() -> None:
    for fragment in (
        "技术校准候选（待人工确认）",
        "必须局部修正",
        "proposal_review_risk==='carrier_included'",
        "局部修正遮罩（必做）",
        "已保存局部修正版",
        "复查局部修正版",
        "遮罩覆盖过大且贴近画面边缘",
        "可能连人台、托盘、展示卡或首饰盒一起保留",
        "仍需逐张确认，不是金标",
        "proposal_coverage_ratio",
        "proposal_border_contact_ratio",
        "x.proposal_coverage_ratio!==null&&x.proposal_coverage_ratio!==undefined",
    ):
        assert fragment in STYLE_SUITE_PAGE


def test_workbench_prioritizes_review_without_promoting_candidates_to_gold() -> None:
    for fragment in (
        "benchmarkReviewPriority",
        "优先放大核对",
        "重点核对",
        "常规核对",
        "需人工框选",
        "没有可靠机器候选，不能猜测商品位置",
        "这里只安排检查顺序，不代表通过或失败",
        "检查顺序：",
        "dataset.reviewPriority",
    ):
        assert fragment in STYLE_SUITE_PAGE
    priority_flow = STYLE_SUITE_PAGE.split(
        "function benchmarkReviewPriority", 1
    )[1].split("async function loadBenchmarkDataset", 1)[0]
    assert "benchmarkReview(" not in priority_flow
    assert "/review" not in priority_flow


def test_workbench_uses_machine_disagreement_only_as_review_order_evidence() -> None:
    for fragment in (
        "benchmarkDisagreementAudits",
        "/review-audit",
        "两套机器候选差异较大",
        "不代表谁错",
        "双机器分歧：",
        "仅为机器复核提示",
        "初筛选中了背景，已反向后比较（原证据未改）",
        "background_inverted_for_comparison",
        "audit.revision===benchmarkDataset.revision",
        "machine_disagreement_urgent",
        "双机器高分歧（优先复核）",
        "machine_disagreement_focused",
        "双机器中分歧（重点复核）",
        "filter==='machine_disagreement_urgent'&&benchmarkReviewPriority(x).score===0",
        "filter==='machine_disagreement_focused'&&benchmarkReviewPriority(x).score===1",
    ):
        assert fragment in STYLE_SUITE_PAGE
    audit_flow = STYLE_SUITE_PAGE.split(
        "async function loadBenchmarkDataset", 1
    )[1].split("benchmarkStateFilter.querySelectorAll", 1)[0]
    assert "method:'POST'" not in audit_flow
    assert "benchmarkReview(" not in audit_flow
    assert audit_flow.index("renderBenchmark()") < audit_flow.index("/review-audit")


def test_workbench_supports_single_sample_navigation_for_disagreement_review() -> None:
    for fragment in (
        'id="benchmark-review-navigator"',
        'aria-label="逐张复核导航"',
        'id="benchmark-review-previous"',
        'id="benchmark-review-position"',
        'id="benchmark-review-next"',
        "benchmarkReviewCursor",
        "renderBenchmarkReviewNavigator",
        "review-focus",
        "is-review-focus",
        "仍需人工确认",
        "benchmarkReviewPrevious.onclick",
        "benchmarkReviewNext.onclick",
    ):
        assert fragment in STYLE_SUITE_PAGE
    navigator_flow = STYLE_SUITE_PAGE.split(
        "function renderBenchmarkReviewNavigator", 1
    )[1].split("async function loadBenchmarkDataset", 1)[0]
    assert "benchmarkReview(" not in navigator_flow
    assert "/review" not in navigator_flow


def test_workbench_compares_original_coarse_and_refined_masks_without_auto_review() -> None:
    for fragment in (
        "renderBenchmarkMachineComparison",
        "benchmark-machine-comparison",
        "data-machine-comparison",
        "原图",
        "初筛遮罩（辅助）",
        "精修遮罩（待确认）",
        "kind=coarse",
        "kind=proposal",
        "sample?.has_coarse_mask",
        "!sample.has_proposed_mask",
        "renderBenchmarkMachineComparison(current,sample)",
    ):
        assert fragment in STYLE_SUITE_PAGE
    comparison_flow = STYLE_SUITE_PAGE.split(
        "function renderBenchmarkMachineComparison", 1
    )[1].split("function renderBenchmarkReviewNavigator", 1)[0]
    assert "method:'POST'" not in comparison_flow
    assert "benchmarkReview(" not in comparison_flow
    assert "/review" not in comparison_flow


def test_workbench_records_queue_triage_and_advances_without_creating_gold() -> None:
    for fragment in (
        "benchmarkReviewDisposition",
        "/review-disposition",
        'data-benchmark-triage="candidate_usable"',
        'data-benchmark-triage="needs_correction"',
        "候选可继续（不是金标）",
        "需要修边",
        "不适合作为样本",
        "review_disposition!=='pending'",
        "这些记录仍不是人工金标",
        "这不是金标确认",
        "await loadBenchmarkDataset(datasetId)",
    ):
        assert fragment in STYLE_SUITE_PAGE
    triage_flow = STYLE_SUITE_PAGE.split(
        "async function benchmarkReviewDisposition", 1
    )[1].split("async function benchmarkLabelSample", 1)[0]
    assert "/gold-mask" not in triage_flow
    assert "benchmarkReview(" not in triage_flow
    assert "human_reviewed" not in triage_flow


def test_workbench_separates_triage_progress_and_correction_queue() -> None:
    for fragment in (
        'id="benchmark-triage-summary"',
        'aria-label="审核处理进度"',
        "review_pending",
        "review_candidate_usable",
        "review_needs_correction",
        "待记录（有遮罩）",
        "候选可继续（未成金标）",
        "需要修边队列",
        "已排除",
        "benchmarkReviewQueueMatch",
        "benchmarkTriageSummary.onclick",
        "data-benchmark-filter",
        "当前队列 0 张",
        "当前队列没有样本",
    ):
        assert fragment in STYLE_SUITE_PAGE
    summary_flow = STYLE_SUITE_PAGE.split(
        "function renderBenchmarkTriageSummary", 1
    )[1].split("renderBenchmarkReviewNavigator=function", 1)[0]
    assert "benchmarkReview(" not in summary_flow
    assert "method:'PATCH'" not in summary_flow
    assert "method:'POST'" not in summary_flow


def test_workbench_keyboard_triage_requires_an_explicit_human_keypress() -> None:
    for fragment in (
        'id="benchmark-keyboard-hint"',
        "<kbd>1</kbd> 候选可继续",
        "<kbd>2</kbd> 需要修边",
        "<kbd>3</kbd> 排除样本",
        "benchmarkTriageBusy",
        "benchmarkRunTriageAction",
        "benchmarkCurrentReviewSample",
        "benchmarkKeyboardShortcut",
        "benchmarkUndoLastTriage",
        "benchmarkUndoTriage.onclick=benchmarkUndoLastTriage",
        "撤销上一条人工选择",
        "disposition:action.reviewDisposition||'pending'",
        "decision:action.suitabilityStatus==='eligible'?'eligible':'pending'",
        "document.addEventListener('keydown',benchmarkKeyboardShortcut)",
    ):
        assert fragment in STYLE_SUITE_PAGE
    shortcut_flow = STYLE_SUITE_PAGE.split(
        "function benchmarkKeyboardShortcut", 1
    )[1].split("document.addEventListener", 1)[0]
    for guard in (
        "event.repeat",
        "benchmarkReviewNavigator.hidden",
        "benchmarkTriageBusy",
        "input,select,textarea,button",
    ):
        assert guard in shortcut_flow
    assert "benchmarkReview(" not in shortcut_flow
    assert "/gold-mask" not in shortcut_flow
    assert "human_reviewed" not in shortcut_flow

    undo_flow = STYLE_SUITE_PAGE.split(
        "async function benchmarkUndoLastTriage", 1
    )[1].split("async function benchmarkLabelSample", 1)[0]
    assert "/review-disposition" in undo_flow
    assert "/suitability" in undo_flow
    assert "/gold-mask" not in undo_flow
    assert "benchmarkReview(" not in undo_flow


def test_workbench_exports_a_read_only_review_receipt() -> None:
    for fragment in (
        "benchmark-export-review-receipt",
        "导出审核回执（JSON）",
        "benchmarkBuildReviewReceipt",
        "foreground-review-receipt-v1",
        "receipt_is_not_human_gold:true",
        "production_accepted:false",
        "source_sha256:sample.source_sha256",
        "review_disposition_reviewer_scope",
        "suitability_reviewer_scope",
        "benchmarkExportReceipt.onclick=benchmarkDownloadReviewReceipt",
        "不包含图片本体",
    ):
        assert fragment in STYLE_SUITE_PAGE
    receipt_flow = STYLE_SUITE_PAGE.split(
        "function benchmarkBuildReviewReceipt", 1
    )[1].split("renderBenchmarkReviewNavigator=function", 1)[0]
    assert "method:'PATCH'" not in receipt_flow
    assert "method:'POST'" not in receipt_flow
    assert "benchmarkReview(" not in receipt_flow
    assert "/gold-mask" not in receipt_flow


def test_workbench_shows_machine_draft_pixel_audit_without_auto_review() -> None:
    for fragment in (
        "自动像素预审提示",
        "机器提示待复查",
        "这不等于遮罩错误",
        "机器草稿像素预审",
        "遮罩碰到画面边界",
        "柔边比例偏高",
        "细链或耳钩疑似断成多段",
        "查看像素预审提示",
        "gold_mask_soft_edge_ratio",
        "gold_mask_component_count",
        "gold_mask_review_risks",
        "仍需人工逐像素确认",
    ):
        assert fragment in STYLE_SUITE_PAGE


def test_workbench_exposes_human_suitability_gate_without_deleting_evidence() -> None:
    for fragment in (
        "待判断样本适用性",
        "已排除但保留证据",
        "确认单件商品，可计入",
        "排除：多商品或无唯一主体",
        "已排除，不计入校准或正式验收；原图和证据仍保留",
        "data-benchmark-suitability-eligible",
        "data-benchmark-suitability-excluded",
        "benchmarkSuitability",
        "/suitability",
    ):
        assert fragment in STYLE_SUITE_PAGE


def test_workbench_supports_a_local_review_deep_link_without_auto_review() -> None:
    assert "benchmark_customer_id" in STYLE_SUITE_PAGE
    assert "benchmark_dataset_id" in STYLE_SUITE_PAGE
    assert "requestedBenchmarkDataset||localStorage.getItem(benchmarkDatasetKey)" in STYLE_SUITE_PAGE
    assert "benchmarkReview(sample)" not in STYLE_SUITE_PAGE.split(
        "const requestedBenchmarkDataset=", 1
    )[1].split("async function normalizeStyleImage", 1)[0]


def test_workbench_supports_one_page_focused_review_without_fake_gold() -> None:
    assert "benchmark_sample_ids" in STYLE_SUITE_PAGE
    assert "benchmark_focus" in STYLE_SUITE_PAGE
    assert "requestedBenchmarkFocus==='review'" in STYLE_SUITE_PAGE
    assert "requestedBenchmarkSampleIds.has(sample.sample_id)" in STYLE_SUITE_PAGE
    assert "document.querySelector('#benchmark-workbench').open=true" in STYLE_SUITE_PAGE
    assert "document.querySelector('.category-readiness').hidden=true" in STYLE_SUITE_PAGE
    assert "集中终审：当前只显示" in STYLE_SUITE_PAGE
    assert "尚未写入人工金标" in STYLE_SUITE_PAGE
    focused_load = STYLE_SUITE_PAGE.split(
        "async function loadBenchmarkDataset", 1
    )[1].split("async function benchmarkUploadSequentially", 1)[0]
    assert "benchmarkReview(" not in focused_load
    assert "/samples/${sample.sample_id}/review" not in focused_load


def test_flat_lay_samples_use_content_aware_prompts_without_auto_review() -> None:
    assert "按图片位置生成遮罩建议" in STYLE_SUITE_PAGE
    assert "data-benchmark-suggest" in STYLE_SUITE_PAGE
    assert "benchmarkSuggestAndPropose" in STYLE_SUITE_PAGE
    assert "/prompt-suggestion" in STYLE_SUITE_PAGE
    assert "suggestion.source_sha256!==sample.source_sha256" in STYLE_SUITE_PAGE
    assert "仍需人工校对，未自动确认" in STYLE_SUITE_PAGE
    assert "sample.category==='earring'" in STYLE_SUITE_PAGE
    suggestion_flow = STYLE_SUITE_PAGE.split(
        "async function benchmarkSuggestAndPropose", 1
    )[1].split("const benchmarkActionGuard", 1)[0]
    assert "benchmarkReview(" not in suggestion_flow


def test_workbench_does_not_force_a_fake_component_for_componentless_products() -> None:
    assert 'name="components" aria-label="关键部件"' in STYLE_SUITE_PAGE
    assert (
        'name="components" aria-label="关键部件" '
        'placeholder="clasp,pendant" required'
    ) not in STYLE_SUITE_PAGE
    assert "!sample.category||!sample.critical_component_names.length" not in STYLE_SUITE_PAGE
    assert "请先保存类别和难度标注" in STYLE_SUITE_PAGE


def test_gold_mask_language_stays_inside_private_benchmark_workbench() -> None:
    normal_generator = STYLE_SUITE_PAGE.split(
        '<section class="workspace-card generator-card">', 1
    )[1].split("</main>", 1)[0]
    assert "人工金标" not in normal_generator
    assert "上传金标" not in normal_generator
    assert "绘制金标" not in normal_generator


def test_page_identifies_sealed_dual_model_pipeline_without_auto_confirmation() -> None:
    assert "version.pipeline_manifest_sha256" in STYLE_SUITE_PAGE
    assert "已封存 SAM 2.1 + BiRefNet 双模型候选" in STYLE_SUITE_PAGE
    assert "version.extractor_version" in STYLE_SUITE_PAGE
    assert "本机基线；仅用于流程测试" in STYLE_SUITE_PAGE
    assert "仍需人工确认" in STYLE_SUITE_PAGE
    assert "自动确认商品抠图" not in STYLE_SUITE_PAGE
    assert "huggingface.co" not in STYLE_SUITE_PAGE
    assert "github.com/facebookresearch/sam2" not in STYLE_SUITE_PAGE
