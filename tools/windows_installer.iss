; Build with tools/build_installer.py using Inno Setup 7.
#ifndef AppVersion
  #error AppVersion must be supplied by the Python build script.
#endif
#define AppTitle "星露谷 MOD 管家"
#define AppExe "星露谷MOD管家.exe"

[Setup]
AppId={{C165E304-18D5-4827-86D0-1E2989B1E739}
AppName={#AppTitle}
AppVersion={#AppVersion}
AppVerName={#AppTitle} {#AppVersion}
AppPublisher=HONGxuanzhao
AppPublisherURL=https://github.com/HONGxuanzhao/StardewModSteward
AppSupportURL=https://github.com/HONGxuanzhao/StardewModSteward/issues
AppUpdatesURL=https://github.com/HONGxuanzhao/StardewModSteward/releases/latest
DefaultDirName={localappdata}\Programs\StardewModSteward
DefaultGroupName={#AppTitle}
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible and not arm64
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile=..\app.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\dist
OutputBaseFilename=StardewModSteward-v{#AppVersion}-windows-x64-Setup
Compression=lzma2
SolidCompression=yes
AppMutex=Local\StardewModSteward.Desktop
SetupMutex=Local\StardewModSteward.Setup
CloseApplications=no
RestartApplications=no
RestartIfNeededByRun=no
Uninstallable=yes
CreateUninstallRegKey=yes
UsePreviousAppDir=yes
UsePreviousTasks=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："
Name: "startmenuicon"; Description: "创建开始菜单快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "..\dist\星露谷MOD管家\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userdesktop}\星露谷MOD管家"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userprograms}\星露谷MOD管家\星露谷MOD管家"; Filename: "{app}\{#AppExe}"; Tasks: startmenuicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "启动星露谷 MOD 管家"; Flags: nowait postinstall skipifsilent

[Messages]
chinesesimp.WelcomeLabel2=安装程序将安装 [name/ver]。%n%n软件已包含运行环境，无需安装 Python。安装前请先关闭已打开的管家。%n%n设置、编组和游戏 MOD 会保留；卸载只移除安装的程序文件。
