import { useEffect, useState } from 'react';
import { ServerAPI } from 'decky-frontend-lib';

export type Settings = {
  autoscan: boolean;
  customSites: string;
  playtimeEnabled: boolean;
  thememusicEnabled: boolean;
  removeShortcutOnUninstall: boolean;
};

export const useSettings = (serverApi: ServerAPI) => {
  const [settings, setSettings] = useState<Settings>({
    autoscan: false,
    customSites: "",
    playtimeEnabled: true,
    thememusicEnabled: true,
    removeShortcutOnUninstall: false,
  });

  // Load saved settings on mount
  useEffect(() => {
    const getData = async () => {
      const savedSettings = (
        await serverApi.callPluginMethod('get_setting', {
          key: 'settings',
          default: settings
        })
      ).result as Settings;
      // Merge over the defaults so settings saved by an older version
      // (without the newer keys) don't leave fields undefined.
      setSettings((prev) => ({ ...prev, ...savedSettings }));
    };
    getData();
  }, [serverApi]);

  // Generic update helper
  async function updateSettings(
    key: keyof Settings,
    value: Settings[keyof Settings]
  ) {
    setSettings((oldSettings) => {
      const newSettings = { ...oldSettings, [key]: value };
      serverApi.callPluginMethod('set_setting', {
        key: 'settings',
        value: newSettings
      });
      return newSettings;
    });
  }

  // Setters
  function setAutoScan(value: Settings['autoscan']) {
    updateSettings('autoscan', value);
  }

  function setCustomSites(value: Settings['customSites']) {
    updateSettings('customSites', value);
  }

  function setPlaytimeEnabled(value: Settings['playtimeEnabled']) {
    updateSettings('playtimeEnabled', value);
  }

  function setThemeMusicEnabled(value: Settings['thememusicEnabled']) {
    updateSettings('thememusicEnabled', value);
  }

  function setRemoveShortcutOnUninstall(value: Settings['removeShortcutOnUninstall']) {
    updateSettings('removeShortcutOnUninstall', value);
  }

  return {
    settings,
    setAutoScan,
    setCustomSites,
    setPlaytimeEnabled,
    setThemeMusicEnabled,
    setRemoveShortcutOnUninstall,
  };
};
