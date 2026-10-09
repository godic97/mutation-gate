package shop

object Price {
  def isAdult(age: Int): Boolean = age >= 18

  def discount(total: Int): Int = if (total > 100) total - 10 else total
}
