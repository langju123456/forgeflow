# Android 构建接入：一次确认用清单

本清单仅核对官方网页／包目录；没有下载、安装或新依赖接入。当前没有 Android SDK、aapt、Gradle、adb；系统 java／javac 路径只是 macOS 包装，不能算可用构建环境。现有 Manifest 源码检查已经完成，APK 和设备仍 NOT_RUN。

方案 A 是最小命令行 APK 构建。固定兼容组合为 JDK 17、Gradle 9.6.0、AGP 9.4.0、Build Tools 36.0.0；保留合成 targetSdk 34，使用 API 34 编译资源。[AGP 官方兼容表](https://developer.android.com/build/releases/agp-9-4-0-release-notes)

| 工具与固定版本 | 压缩下载量 | 用途与官方来源 |
| --- | ---: | --- |
| Temurin JDK 17.0.20.1+1，Mac ARM64 | 185,851,019 字节，约 185.85 MB | 编译器／构建 JVM；[Adoptium 元数据](https://api.adoptium.net/v3/assets/latest/17/hotspot) |
| Android Command-line Tools 23.0，ARM64，16111833 | 155,384,151 字节，约 155.38 MB | sdkmanager；[Google SDK 包目录](https://dl.google.com/android/repository/repository2-3.xml) |
| SDK Platform android-34，revision 3 | 63,180,081 字节，约 63.18 MB | android.jar／API 34 编译资源；同一官方包目录 |
| SDK Build Tools 36.0.0，Mac | 79,121,749 字节，约 79.12 MB | aapt2、D8、zipalign、apksigner；同一官方包目录 |
| Gradle 9.6.0，binary-only | 官方列 134.16M，保留该单位 | 构建调度；[Gradle 官方发行目录](https://services.gradle.org/distributions/) |
| Android Gradle Plugin 9.4.0 与必要传递依赖 | 尚无固定依赖树总量 | Android 构建插件；官方兼容表／Google Maven |

前四项精确合计 483,537,000 字节；还要加 Gradle 与插件依赖，不能将它当总下载量。AGP／传递依赖、解压与缓存总量没有统一官方数值，固定最小 Java 项目依赖树后才能确认。下载页仍列旧 CLI 15859902／156.1 MB，安装须锁定表中具体包，而不是模糊 latest。[Android 下载页](https://developer.android.com/studio)

建议授权范围：只下载／解压上述 A 工具到本项目 `.runtime/android-toolchain`，SDK、Gradle cache 与环境变量均只供项目子进程；不写全局 shell 配置、不读私人凭据、不接新 provider、不安装 Android Studio／模拟器／NDK。先核原生 SDK 工具架构；若必须安装 Rosetta，或出现额外系统安装，停止报告，不能从 A 推导出该授权。JDK／CLI 已有 ARM 包，但选定 aapt2／zipalign 的原生架构尚未核实。

资源方案是小型 Java APK、JVM 堆上限 2 GiB、最多两个 worker，不同时做本地模型推理。它只是拟定执行限制，不是实测内存承诺；SDK 解压／Gradle 缓存会占额外磁盘，首次构建会用 CPU 和磁盘。可选择授权下载上限 2 GB、项目工具／缓存磁盘上限 8 GB；这两个数是拟定预算，不是官方包大小。超过预算前暂停并报告。M4／24 GB 的实际峰值需接入后测量。

方案 B 在 A 上增加 Platform Tools 37.0.1，Mac **16,110,554 字节／约 16.11 MB**，用于 adb 安装、启动和日志取证。[官方 Platform Tools](https://developer.android.com/tools/releases/platform-tools)；还需要用户自有 Android 手机、数据线、开启 USB 调试并在手机确认连接。macOS 不需额外 USB 驱动。[实体设备设置](https://developer.android.com/studio/run/device)

费用：这些工具无需付费订阅；Temurin 免费，SDK 不收使用许可费，自有设备验证不产生云机／API 费。可能购机／线材费用不在本授权内。[Temurin FAQ](https://adoptium.net/docs/faq)、[Android SDK 条款](https://developer.android.com/studio)

可省去约 1.5 GB 的 Android Studio ARM 安装包、模拟器、系统镜像、NDK 和新增 GPU。A 还需要在项目中实现最小 Activity／构建文件；构建成功只可关闭 APK 构建缺口，不能关闭 device UNKNOWN。B 才可能取得设备证据；没有手机则保持 UNKNOWN，不能换成虚构 PASS。

待用户决定：A 是否按上面的固定工具、项目目录和预算接入；B 是否有自有设备且授权安装合成演示 APK。未获该决定前继续已有本地契约工作，不安装／下载。
