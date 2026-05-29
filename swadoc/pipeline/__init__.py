"""
Pipeline orchestration and core processing components.

This package contains:
- orchestrator.py: PipelineOrchestrator for end-to-end execution
- quality_checker.py: AnnotationQualityChecker for route classification
- backup.py: Backup manager for safe file modification
- writer.py: AnnotationWriter for inserting annotations
- spec_generator.py: OpenAPI spec generation
- spec_validator.py: OpenAPI spec validation
"""

from swadoc.pipeline.orchestrator import PipelineOrchestrator

__all__ = ["PipelineOrchestrator"]
