"""Throwaway test for the #103 proof: it fails so the gate job goes red. Reverted next."""


def test_gate_goes_red_when_a_job_fails():
    raise AssertionError("deliberate failure to prove the gate job turns red")
