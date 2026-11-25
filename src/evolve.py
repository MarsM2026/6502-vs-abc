"""Small, pedagogical genetic-algorithm prototype.

This is intentionally not a logic-gate simulator. It demonstrates population,
selection, crossover, mutation, and fitness before the much harder circuit stage.
"""

import random
from dataclasses import dataclass


@dataclass
class Candidate:
    genes: list[int]


POPULATION_SIZE = 40
GENOME_SIZE = 256


def seed_population(size=POPULATION_SIZE) -> list[Candidate]:
    return [Candidate([random.randint(0, 1) for _ in range(GENOME_SIZE)]) for _ in range(size)]


def fitness(candidate: Candidate) -> float:
    """Toy score: reward a candidate for a binary lookup-table-like target."""
    correct = 0
    tests = 0
    for a in range(0, 256, 17):
        for b in range(0, 256, 17):
            target = (a + b) & 255
            produced = target if candidate.genes[(a + b) & 255] else 0
            correct += produced == target
            tests += 1
    return correct / tests


def tournament(population: list[Candidate], size=5) -> Candidate:
    contestants = random.sample(population, size)
    return max(contestants, key=fitness)


def crossover(parent_a: Candidate, parent_b: Candidate) -> Candidate:
    cut = random.randrange(GENOME_SIZE)
    return Candidate(parent_a.genes[:cut] + parent_b.genes[cut:])


def mutate(candidate: Candidate, count=None) -> Candidate:
    if count is None:
        count = random.randint(1, 3)
    for _ in range(count):
        position = random.randrange(GENOME_SIZE)
        candidate.genes[position] ^= 1
    return candidate


def evolve(generations=20) -> tuple[list[Candidate], list[float]]:
    population = seed_population()
    history = []
    for generation in range(generations):
        population.sort(key=fitness, reverse=True)
        best = fitness(population[0])
        history.append(best)
        print(f"Generation {generation:2d}: {best:.3f}")

        elites = [Candidate(candidate.genes.copy()) for candidate in population[:8]]
        while len(elites) < POPULATION_SIZE:
            child = crossover(tournament(population), tournament(population))
            elites.append(mutate(child))
        population = elites
    return population, history


if __name__ == "__main__":
    evolve()
