"""Pipeline routes (mounted under api/pipeline/)."""

from django.urls import path

from pipeline.views import (
    AnswerView,
    ApproveView,
    CancelView,
    DownloadView,
    ExportView,
    FileDetailView,
    FileListView,
    OfferListView,
    PreviewCancelView,
    PreviewCreateView,
    PreviewConfirmView,
    PreviewRenderView,
    QuoteDownloadView,
    QuoteView,
    RemapView,
    ReviewListView,
    RuleDetailView,
    RuleListView,
    RunDetailView,
    UploadView,
)

urlpatterns = [
    path("uploads/", UploadView.as_view(), name="upload"),
    path("previews/", PreviewCreateView.as_view(), name="preview-create"),
    path(
        "previews/<str:key>/render/",
        PreviewRenderView.as_view(),
        name="preview-render",
    ),
    path(
        "previews/<str:key>/confirm/",
        PreviewConfirmView.as_view(),
        name="preview-confirm",
    ),
    path("previews/<str:key>/", PreviewCancelView.as_view(), name="preview-cancel"),
    path("files/", FileListView.as_view(), name="files"),
    path("files/<int:file_id>/", FileDetailView.as_view(), name="file-detail"),
    path("rules/", RuleListView.as_view(), name="rules"),
    path("rules/<int:rule_id>/", RuleDetailView.as_view(), name="rule-detail"),
    path("runs/<int:run_id>/", RunDetailView.as_view(), name="run-detail"),
    path("runs/<int:run_id>/offers/", OfferListView.as_view(), name="offers"),
    path("runs/<int:run_id>/reviews/", ReviewListView.as_view(), name="reviews"),
    path("runs/<int:run_id>/approve/", ApproveView.as_view(), name="approve"),
    path("runs/<int:run_id>/export/", ExportView.as_view(), name="export"),
    path("runs/<int:run_id>/cancel/", CancelView.as_view(), name="cancel"),
    path("runs/<int:run_id>/remap/", RemapView.as_view(), name="remap"),
    path("runs/<int:run_id>/quote/", QuoteView.as_view(), name="quote"),
    path(
        "runs/<int:run_id>/quote/download/",
        QuoteDownloadView.as_view(),
        name="quote-download",
    ),
    path(
        "runs/<int:run_id>/quote/download/<str:file_format>/",
        QuoteDownloadView.as_view(),
        name="quote-download-format",
    ),
    path("reviews/<int:review_id>/answer/", AnswerView.as_view(), name="answer"),
    path(
        "artifacts/<int:artifact_id>/download/",
        DownloadView.as_view(),
        name="download",
    ),
]
