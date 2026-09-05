<script lang="ts">
  import { SettingsLayout } from '$lib/components/settings';
  import TransactionRulesCard from '$lib/components/money/TransactionRulesCard.svelte';
  import TransactionRuleTestCard from '$lib/components/money/TransactionRuleTestCard.svelte';
  import TransactionRuleCoverageCard from '$lib/components/money/TransactionRuleCoverageCard.svelte';

  // Each card loads its own data and owns its own writes, so this page is the
  // frame and nothing else. The order is the working order: the rules, then a
  // way to try them, then what the imports actually carried.
</script>

<SettingsLayout
  title="Transactions"
  description="What an imported transaction posts to. One ordered pass of rules per transaction, shared by the API sync and a CSV import, so both resolve the same way."
>
  <TransactionRulesCard />
  <TransactionRuleTestCard />
  <TransactionRuleCoverageCard />
</SettingsLayout>
