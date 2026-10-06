---
name: android-manifest
description: Parse the controlled synthetic Android manifest and compare its explicit source values with the bound interface contract.
allowed-tools: domain.artifacts.read
metadata:
  version: domain-source-review-v1
---

Parse bounded XML with the Android attribute namespace. Reject malformed XML, DTD/entity declarations and duplicate relevant declarations.
Check package, explicit integer minimum/target SDK, BOM/protocol metadata, launcher identity, debuggable and exported values.
For target SDK 31 or above, an activity/service/receiver with an intent filter must explicitly declare android:exported.
SDK 26/34, BOM A3 and protocol 1 are controlled synthetic fixture values, not recommendations for current production development.
Record build/device evidence as UNKNOWN with NOT_RUN_NO_ANDROID_TOOLCHAIN. Successful source checks cannot imply compilation, installation, runtime compatibility or device testing.
This is a deterministic SOP, not an LLM agent. Do not download tools, invoke a build, approve or change project stages.

Source rule: https://developer.android.com/about/versions/12/behavior-changes-12#exported
