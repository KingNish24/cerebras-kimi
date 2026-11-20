import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

import ModelSelector from '@/components/chat/home/ModelSelector';
import type { LLMOption } from '@/lib/llm-options';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/hooks/use-linger-expand', () => ({
  useLingerExpand: () => ({ expanded: true, linger: vi.fn(), triggerProps: {} }),
}));
vi.mock('@/hooks/use-outside-click', () => ({ useOutsideClick: () => {} }));
vi.mock('@/shared/ui/Tooltip', () => ({
  default: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock('@/components/common/ProviderIcon', () => ({
  default: () => <span data-testid="provider-icon" />,
}));

function option(profileId: string, modelId: string, overrides: Partial<LLMOption> = {}): LLMOption {
  return {
    profile_id: profileId,
    model_id: modelId,
    model: modelId,
    model_name: modelId,
    profile_name: 'WuS Router',
    provider: 'wusrouter',
    provider_label: 'WuS Router',
    is_active_default: false,
    ...overrides,
  } as LLMOption;
}

it('keeps the selected model in the short recent-only menu', () => {
  const selected = option('wusrouter', 'wr/deepseek-v4.1-flash:free', {
    is_active_default: true,
  });
  render(
    <ModelSelector
      options={[
        option('wusrouter', 'codex/gpt-5:free', { is_recent: true }),
        option('wusrouter', 'ws/gpt-5.6-terra:free', { is_recent: true }),
        selected,
      ]}
      activeDefault={{ profile_id: selected.profile_id, model_id: selected.model_id }}
      value={{ profile_id: selected.profile_id, model_id: selected.model_id }}
      loading={false}
      error={false}
      onChange={vi.fn()}
    />,
  );

  const trigger = screen.getByRole('button', { name: 'Select model' });
  expect(trigger.textContent).toContain('wr/deepseek-v4.1-flash:free');
  fireEvent.click(trigger);

  // The selected model is not flagged recent, but must still be reachable so
  // the user can see which model is driving the conversation.
  expect(screen.getByRole('button', { name: 'wr/deepseek-v4.1-flash:free | WuS Router' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'codex/gpt-5:free | WuS Router' })).toBeTruthy();
});

it('shows a legacy slug selection as its catalog model', () => {
  const kilo = option('llm-profile-kilo', 'llm-model-kilo-auto', {
    model: 'kilo-auto/free',
    model_name: 'kilo-auto/free',
    provider: 'kilo',
    profile_name: 'Kilo Gateway',
    provider_label: 'Kilo Gateway',
  });
  render(
    <ModelSelector
      options={[
        option('wusrouter', 'codex/gpt-5:free', { is_recent: true }),
        option('wusrouter', 'wr/deepseek-v4.1-flash:free', { is_recent: true }),
        kilo,
      ]}
      activeDefault={{ profile_id: 'wusrouter', model_id: 'wr/deepseek-v4.1-flash:free' }}
      value={{ profile_id: 'kilo', model_id: 'kilo-auto/free' }}
      loading={false}
      error={false}
      onChange={vi.fn()}
    />,
  );

  const trigger = screen.getByRole('button', { name: 'Select model' });
  expect(trigger.textContent).toContain('kilo-auto/free');
  fireEvent.click(trigger);
  expect(screen.getByRole('button', { name: 'kilo-auto/free | Kilo Gateway' })).toBeTruthy();
});
